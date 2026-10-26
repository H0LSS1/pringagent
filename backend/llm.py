"""LLM 与 Embedding 的统一封装（阶段三智能体的大脑）。

【零基础说明：LLM 怎么调用？】
- 大模型服务（智谱 GLM）提供 HTTP API，官方推荐用 openai 这个 SDK 调用
  （因为智谱兼容 OpenAI 的接口格式，只要把 base_url 换成智谱的地址即可）。
- 两种调用：
  1) chat()：对话补全。输入一组消息（system/user/assistant），返回回答文本。
     智能体的"意图识别""回答生成""SQL 生成"都靠它。
  2) embed_texts()：文本向量化。把一段文字变成 1024 个小数组成的向量
     （可以理解为文字的"坐标"），两段文字意思越近，坐标越接近。
     RAG 检索就是靠"问题坐标"和"知识块坐标"的距离找相关内容。

【注意事项】
- API Key 在 backend/.env 里（不要提交到 Git！）
- embeddings 接口一次最多传一批文本，我们按 16 条一批分批调用
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import openai
from openai import OpenAI

from config import settings

log = logging.getLogger("llm")

_client: OpenAI | None = None

# embedding 批大小：一次请求带多少条文本（太大容易超时/超限）
EMBED_BATCH = 16
# 限流/服务波动重试：公共模型服务偶尔 429（负载饱和），指数退避重试即可
MAX_RETRIES = 3


def get_llm_client() -> OpenAI:
    """全局唯一的 OpenAI 兼容客户端（指向智谱开放平台）。"""
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    return _client


def chat(messages: list[dict], temperature: float = 0.3,
         max_tokens: int = 1024, json_mode: bool = False,
         think: bool = False) -> str:
    """调用对话模型，返回回答文本。限流/网络抖动自动重试（1s→2.5s→6s）。

    参数说明：
    - messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
      system=给模型的指令/人设；user=用户的话；assistant=模型之前说的话
    - temperature: 0=最稳定（适合 SQL/分类），越大越有创造性（适合聊天）
    - json_mode=True 时强制模型输出 JSON（意图识别等结构化场景用）
    - think=False 关闭"深度思考"：glm-4.7 系列默认先输出一段思考
      （reasoning_content），思考太长会把 max_tokens 吃光导致正文为空；
      本项目的意图分类/SQL 生成/解读都不需要思考，关闭后更快更稳。
    """
    kwargs: dict = dict(model=settings.llm_model, messages=messages,
                        temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if not think:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    attempt = 0
    delay = 1.0
    while attempt < MAX_RETRIES:
        try:
            resp = get_llm_client().chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            if not content:
                # 思考型模型偶发只返回思考没返回正文：当作失败走重试
                raise ValueError("empty content from model (thinking consumed budget?)")
            return content
        except openai.BadRequestError as exc:
            # 部分模型强制思考、不支持关闭（如 glm-5.3-flash，错误码 1210）：
            # 自动回退为开思考并加大 token 预算（思考要占 token），该回退不计入重试次数
            thinking = (kwargs.get("extra_body") or {}).get("thinking", {})
            if thinking.get("type") == "disabled":
                log.warning("model %s rejects thinking=disabled (%s); retrying with thinking=enabled",
                            settings.llm_model, str(exc)[:120])
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                kwargs["max_tokens"] = max(kwargs["max_tokens"], 2048)
                continue
            raise
        except (openai.RateLimitError, openai.APIConnectionError,
                openai.InternalServerError, ValueError) as exc:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise
            log.warning("LLM call failed (attempt %d/%d): %r, retry in %.1fs",
                        attempt, MAX_RETRIES, exc, delay)
            time.sleep(delay)
            delay *= 2.5


def chat_stream(messages: list[dict], temperature: float = 0.2,
                max_tokens: int = 2048, think: bool = False) -> Iterator[str]:
    """流式版 chat()：逐段 yield 回答文本（前端 SSE 边收边渲染）。

    返回一个生成器；仅在"一个字都没产出"时才重试（避免重试导致重复输出）。
    思考模式处理与 chat() 相同：不支持关闭的模型自动回退并加大 token 预算。
    """
    kwargs: dict = dict(model=settings.llm_model, messages=messages,
                        temperature=temperature, max_tokens=max_tokens, stream=True)
    if not think:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    def _generate() -> Iterator[str]:
        attempt = 0
        delay = 1.0
        while attempt < MAX_RETRIES:
            produced = False
            try:
                stream = get_llm_client().chat.completions.create(**kwargs)
                for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        produced = True
                        yield delta
                return
            except openai.BadRequestError as exc:
                thinking = (kwargs.get("extra_body") or {}).get("thinking", {})
                if thinking.get("type") == "disabled":
                    log.warning("model %s rejects thinking=disabled (%s); retrying with thinking=enabled",
                                settings.llm_model, str(exc)[:120])
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    kwargs["max_tokens"] = max(kwargs["max_tokens"], 4096)
                    continue
                raise
            except (openai.RateLimitError, openai.APIConnectionError,
                    openai.InternalServerError, ValueError) as exc:
                # 已经吐出过内容再失败：重试会导致重复输出，直接抛给上层兜底
                if produced or attempt >= MAX_RETRIES - 1:
                    raise
                attempt += 1
                log.warning("LLM stream failed (attempt %d/%d): %r, retry in %.1fs",
                            attempt, MAX_RETRIES, exc, delay)
                time.sleep(delay)
                delay *= 2.5

    return _generate()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """把一批文本转成向量列表（顺序与输入一致）。自动分批，调用方无感。

    dimensions 必须显式传：embedding-3 默认输出 2048 维，
    而我们 .env/Milvus 集合统一用 1024 维（维度必须一致才能入库检索）。
    """
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        resp = get_llm_client().embeddings.create(
            model=settings.embedding_model, input=batch,
            dimensions=settings.embedding_dim)
        vectors.extend(d.embedding for d in resp.data)
    return vectors
