"""知识库入库：文档 -> 分块 -> 向量化 -> 写入 Milvus + MySQL。

【零基础说明：RAG 的"R"（检索）是怎么准备出来的？】
1. 读文档：rag/knowledge/ 下的 PDF（能提取多少提多少）+ 人工知识条目
   （knowledge_seed.py，中文国标 PDF 提不出字，靠它保底）
2. 分块（chunking）：把长文本切成 400 字左右的小块，相邻块重叠 80 字。
   为什么要切块？向量检索是"块"级别的——块太大则一块里混多个主题，
   检索不准；块太小则上下文不完整。
3. 向量化（embedding）：每个块调用 GLM embedding-3 变成 1024 维向量
4. 写入 Milvus：向量数据库，支持"找离查询向量最近的 K 个块"
5. 写入 MySQL knowledge_chunks 表：保存原文与编号，
   供 BM25 关键词检索与答案引用溯源使用

用法（backend 目录下）：
    .venv\\Scripts\\python -m rag.ingest            # 全量重建知识库
    .venv\\Scripts\\python -m rag.ingest --dry-run  # 只看能提取到多少块，不入库
"""

import argparse
import logging
import re
from pathlib import Path

from pymilvus import DataType, MilvusClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from config import settings
from db.models import KnowledgeChunk
from db.mysql import get_session_factory
from llm import embed_texts
from rag.knowledge_seed import SEED_ENTRIES
from rag.pdf_loader import load_pdf_pages

log = logging.getLogger("rag.ingest")

# Milvus 集合名（相当于关系库的"表名"）
COLLECTION = "print_knowledge"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# 分块参数：块长 400 字、重叠 80 字（中文标准文档的常用配置）
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

# 条款号模式：用于从块里识别"出处"（如 5.2.1、表 3、Chapter 4）
_SECTION_RE = re.compile(r"((?:第[一二三四五六七八九十\d]+[章节条])|\d+(?:\.\d+){1,3}|[表附]?\s?\d+)")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把长文本切成有重叠的块。

    重叠（overlap）的作用：如果一句话正好被切在边界上，
    下一块的开头仍包含它的结尾，语义不会断。
    """
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    step = size - overlap
    return [text[i:i + size] for i in range(0, len(text), step)]


def guess_section(text: str) -> str:
    """从块文本里猜条款号（给答案溯源用，猜不到返回 '正文'）。"""
    m = _SECTION_RE.search(text)
    return m.group(1) if m else "正文"


def build_documents() -> list[dict]:
    """汇总所有知识来源：PDF 每页 + 人工条目。返回 [{doc, section, text}]。"""
    docs: list[dict] = []
    # ---- 1. PDF 文档 ----
    for pdf in sorted(KNOWLEDGE_DIR.glob("*.pdf")):
        pages = load_pdf_pages(pdf)
        log.info("pdf %s -> %d usable pages", pdf.name, len(pages))
        for i, page_text in enumerate(pages, start=1):
            docs.append({"doc": pdf.stem.replace("+", "/"),
                         "section": f"page {i}", "text": page_text})
    # ---- 2. 人工知识条目 ----
    for entry in SEED_ENTRIES:
        docs.append({"doc": entry["doc"], "section": entry["section"],
                     "text": entry["text"]})
    return docs


def build_chunks(docs: list[dict]) -> list[dict]:
    """把文档列表切成块：[{doc, section, chunk_index, text}]。

    chunk_index 在同一文档内累加（PDF 每页是一个独立 doc 条目，
    不能每页都从 0 数，否则 (doc_name, chunk_index) 唯一键会冲突）。
    """
    chunks: list[dict] = []
    counter: dict[str, int] = {}
    for d in docs:
        for piece in chunk_text(d["text"]):
            idx = counter.get(d["doc"], 0)
            counter[d["doc"]] = idx + 1
            chunks.append({
                "doc": d["doc"],
                "section": guess_section(piece) if d["section"] == "正文" else d["section"],
                "chunk_index": idx,
                "text": piece,
            })
    return chunks


def ensure_collection(client: MilvusClient) -> None:
    """重建 Milvus 集合（全量入库前先删旧集合，保证没有脏数据）。"""
    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
    # 定义集合结构：自增主键 + 向量列 + 元数据列
    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("pk", DataType.INT64, is_primary=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=settings.embedding_dim)
    schema.add_field("text", DataType.VARCHAR, max_length=8192)
    schema.add_field("doc", DataType.VARCHAR, max_length=512)
    schema.add_field("section", DataType.VARCHAR, max_length=128)
    # 索引参数：AUTOINDEX 由 Milvus 自选索引类型；COSINE 余弦相似度（1=意思一样）
    index = client.prepare_index_params()
    index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
    client.create_collection(COLLECTION, schema=schema, index_params=index)


def ingest(dry_run: bool = False) -> None:
    """主流程：取文档 -> 分块 -> 向量化 -> 双写 Milvus 与 MySQL。"""
    docs = build_documents()
    chunks = build_chunks(docs)
    print(f"[ingest] documents={len(docs)} chunks={len(chunks)}")
    if dry_run or not chunks:
        for c in chunks[:10]:
            print(f"  - [{c['doc']}|{c['section']}] {c['text'][:60]}...")
        return

    # ---- 向量化（分批在 embed_texts 内部处理） ----
    print("[ingest] embedding...")
    vectors = embed_texts([c["text"] for c in chunks])
    print(f"[ingest] vectors ready: dim={len(vectors[0]) if vectors else 0}")

    # ---- 写 Milvus ----
    client = MilvusClient(uri=settings.milvus_uri)
    ensure_collection(client)
    rows = [{"vector": vec, "text": c["text"], "doc": c["doc"], "section": c["section"]}
            for c, vec in zip(chunks, vectors)]
    # 返回值形如 {"insert_count": N, "ids": [...]}：auto_id 生成的主键，顺序与插入一致
    result = client.insert(collection_name=COLLECTION, data=rows)
    milvus_pks = [str(x) for x in result["ids"]]

    # ---- 写 MySQL（原文 + Milvus 主键，检索融合与溯源用） ----
    with Session(get_session_factory().kw["bind"]) as session:
        session.execute(delete(KnowledgeChunk))  # 全量重建
        session.execute(insert(KnowledgeChunk), [
            {"doc_name": c["doc"], "chunk_index": c["chunk_index"], "content": c["text"],
             "milvus_pk": pk, "embedding_model": settings.embedding_model}
            for c, pk in zip(chunks, milvus_pks)
        ])
        session.commit()
        n = session.execute(select(func.count()).select_from(KnowledgeChunk)).scalar()
    print(f"[ingest] done: milvus collection '{COLLECTION}' + mysql knowledge_chunks={n}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="知识库入库")
    ap.add_argument("--dry-run", action="store_true", help="只统计与预览，不入库")
    args = ap.parse_args()
    ingest(dry_run=args.dry_run)
