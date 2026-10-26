"""混合检索器：BM25 关键词召回 + Milvus 向量召回 + RRF 融合 + 启发式重排。

【零基础说明：为什么要"混合"检索？】
单一检索方式各有盲区：
- 向量检索（语义）：懂"换个说法"——问"颜色差多少"能召回"色差 ΔE"的内容，
  但对精确词（标准号、专有名词如 GB/T 9851）不敏感；
- BM25 检索（关键词）：对精确词/编号很强，但换个说法就搜不到。
两者互补，各取前 K 名，用 RRF（Reciprocal Rank Fusion）融合：
    RRF 分数 = Σ 1/(60 + 该通道内的名次)
名次越靠前加分越多，两个通道都命中的块总分最高——这是工业界最常用的
轻量融合公式，不需要训练。

【重排（rerank）】
融合后再用启发式规则微调顺序：查询词在正文出现越多、整词命中越多，越靠前。
（生产环境通常用交叉编码器模型做重排，这里为控制延迟用规则实现。）

【溯源】
每个结果带 doc（文档名）+ section（条款/页码），答案生成时要求模型标注引用，
实现"答案附条款溯源"。
"""

import logging
import re

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from db.models import KnowledgeChunk
from db.mysql import get_session_factory
from llm import embed_texts

log = logging.getLogger("rag.retriever")

# 检索参数：每通道召回 2×top_k，融合后重排取 top_k
RECALL_K = 12
RRF_K = 60  # RRF 平滑常数（论文经验值）

# 中文停用词（jieba 分出来的"的/是/了"之类对检索无意义的词）
_STOPWORDS = {"的", "了", "是", "在", "和", "与", "及", "或", "对", "对于", "什么",
              "多少", "怎么", "如何", "请", "你", "我", "他", "这", "那", "有", "吗",
              "呢", "啊", "把", "被", "给", "让", "说", "要", "会", "能", "可以"}


def tokenize(text: str) -> list[str]:
    """中文分词 + 去停用词 + 去单字符噪声（BM25 与关键词匹配共用）。"""
    words = jieba.lcut(text.lower())
    return [w for w in words if w.strip() and w not in _STOPWORDS and len(w) > 1]


class HybridRetriever:
    """启动时从 MySQL 加载全部知识块建立 BM25 索引；向量侧走 Milvus。

    知识库规模（几百块）在内存里建 BM25 毫无压力；如果未来到百万级，
    应换 Milvus 的 BM25 稀疏向量或 Elasticsearch。
    """

    def __init__(self):
        self._chunks: list[dict] = []      # [{pk, doc, section, content}]
        self._bm25: BM25Okapi | None = None
        self._corpus_tokens: list[list[str]] = []
        self.reload()

    def reload(self) -> None:
        """从 MySQL 读知识块并重建 BM25 索引（ingest 之后调用）。"""
        with Session(get_session_factory().kw["bind"]) as session:
            rows = session.execute(
                select(KnowledgeChunk).order_by(KnowledgeChunk.id)
            ).scalars().all()
        self._chunks = [{"pk": r.milvus_pk, "doc": r.doc_name, "section": r.chunk_index,
                         "content": r.content} for r in rows]
        self._corpus_tokens = [tokenize(c["content"]) for c in self._chunks]
        self._bm25 = BM25Okapi(self._corpus_tokens) if self._chunks else None
        log.info("retriever ready: %d chunks indexed", len(self._chunks))

    @property
    def size(self) -> int:
        return len(self._chunks)

    # ---------- 两个召回通道 ----------

    def _bm25_search(self, query: str) -> dict[str, float]:
        """BM25 通道：返回 {pk: 名次}。"""
        if not self._bm25:
            return {}
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(zip(self._chunks, scores), key=lambda x: x[1], reverse=True)
        # 只要有分的（BM25 分数 0 = 完全不相关）
        return {c["pk"]: rank for rank, (c, s) in enumerate(ranked[:RECALL_K]) if s > 0}

    def _vector_search(self, query: str) -> dict[str, float]:
        """向量通道：问题 -> 向量 -> Milvus 找最近邻。返回 {pk: 名次}。"""
        from pymilvus import MilvusClient
        vec = embed_texts([query])[0]
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            hits = client.search(
                collection_name="print_knowledge",
                data=[vec],
                limit=RECALL_K,
                output_fields=["doc", "section"],
            )[0]
            # pymilvus 2.5 的命中结构：{"pk": 主键, "distance": 相似度, "entity": {...}}
            return {str(h["pk"]): rank for rank, h in enumerate(hits)}
        finally:
            client.close()

    # ---------- 融合 + 重排 ----------

    def search(self, query: str, top_k: int = 6) -> list[dict]:
        """混合检索主入口。返回 [{doc, section, content, score, sources}]。"""
        bm_rank = self._bm25_search(query)
        try:
            vec_rank = self._vector_search(query)
        except Exception as exc:  # noqa: BLE001 向量库挂了不能影响整体，退化为纯 BM25
            log.warning("vector search failed, fallback to bm25 only: %r", exc)
            vec_rank = {}

        # RRF 融合：两个通道的名次 -> 统一分数
        rrf: dict[str, float] = {}
        for pk, rank in bm_rank.items():
            rrf[pk] = rrf.get(pk, 0.0) + 1.0 / (RRF_K + rank + 1)
        for pk, rank in vec_rank.items():
            rrf[pk] = rrf.get(pk, 0.0) + 1.0 / (RRF_K + rank + 1)

        pk2chunk = {c["pk"]: c for c in self._chunks}
        candidates = [(pk, s) for pk, s in rrf.items() if pk in pk2chunk]
        # 启发式重排：整词命中的候选加分（用检索词表在原文里数命中次数）
        q_tokens = set(tokenize(query))
        def rerank_score(item):
            pk, s = item
            content = pk2chunk[pk]["content"]
            hit = sum(1 for t in q_tokens if t in content)
            return s * (1.0 + 0.15 * hit)
        candidates.sort(key=rerank_score, reverse=True)

        results = []
        for pk, s in candidates[:top_k]:
            c = pk2chunk[pk]
            results.append({
                "doc": c["doc"],
                "section": str(c["section"]),
                "content": c["content"],
                "score": round(s, 4),
                "sources": ["bm25"] * (pk in bm_rank) + ["vector"] * (pk in vec_rank),
            })
        return results


# 模块级单例：API 进程里只建一次（BM25 索引加载开销不小）
_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever
