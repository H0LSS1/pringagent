"""智能体对话接口：SSE 流式响应 + 长期会话记忆。

【零基础说明：本文件解决两件事】
1. 长期记忆：对话按 session 存 MySQL（chat_sessions/chat_messages 表）。
   前端带着 session_id 来，就能翻出整段历史——换页面、关浏览器再回来都在。
2. 流式输出：最终回答用 llm.chat_stream 逐段生成，SSE 以 answer_delta
   一小段一小段推给前端（打字机效果）；工具节点（检索/SQL）的进度事件
   在回答开始前就推出去，用户随时知道进展到哪一步。

事件流（每条 "data: {json}\n\n"）：
    session    -> 告知 session_id（前端存起来，下一轮带回来）
    node       -> LangGraph 走到哪个节点（route/knowledge/sql/prepare）
    status     -> 进度文本（"检索到 N 条相关知识"）
    citations  -> 知识引用（溯源卡片）
    sql        -> 生成的 SQL（含执行成败与尝试次数）
    table      -> 查询结果表
    chart      -> ECharts 图表配置
    answer_delta -> 回答的增量文本片段（流式核心）
    answer     -> 回答全文（兜底覆盖）
    done       -> 结束
"""

import json
import logging

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.graph import graph
from db.models import ChatMessage, ChatSession
from db.mysql import get_session_factory
from llm import chat_stream

log = logging.getLogger("api.chat")

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    """POST /api/chat/stream 请求体。session_id 不传则新建会话。"""
    message: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    session_id: int | None = Field(None, description="会话 id（不传=新建会话）")
    history: list[dict] = Field(default_factory=list, description="前端带来的最近对话")


def _sse(obj: dict) -> str:
    """把一条事件编码成 SSE 帧文本：data: {...}\\n\\n"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _get_or_create_session(session_id: int | None, title: str) -> int:
    """拿到会话 id：不存在则新建（前端传了失效 id 也兜底新建）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        if session_id is not None and session.get(ChatSession, session_id) is not None:
            return session_id
        sess = ChatSession(title=title[:50])
        session.add(sess)
        session.commit()
        return sess.id


def chat_stream_api(req: ChatRequest):
    """SSE 生成器：驱动 LangGraph -> 流式生成回答 -> 全程落库。"""

    def generate():
        session_id = _get_or_create_session(req.session_id, req.message)
        yield _sse({"type": "session", "session_id": session_id})

        # 用户消息先落库
        with Session(get_session_factory().kw["bind"]) as db:
            db.add(ChatMessage(session_id=session_id, role="user", content=req.message))
            db.commit()

        # ---- 驱动工作流：每个节点完成立即推送进度 ----
        state = {"question": req.message, "history": req.history}
        final_state: dict = {}
        for update in graph.stream(state, stream_mode="updates"):
            for node, delta in update.items():
                final_state.update(delta)
                yield _sse({"type": "node", "name": node})
                if node == "knowledge" and delta.get("retrieved"):
                    yield _sse({"type": "status", "text": f"检索到 {len(delta['retrieved'])} 条相关知识"})
                if node == "sql" and delta.get("sql_result"):
                    sr = delta["sql_result"]
                    yield _sse({"type": "sql", "sql": sr["sql"],
                                "ok": sr["ok"], "attempts": sr["attempts"],
                                "error": sr["error"]})
                    if sr["ok"]:
                        yield _sse({"type": "table", "columns": sr["columns"],
                                    "rows": sr["rows"], "note": sr["note"]})
                # 图表配置由 sql 节点产出
                if node == "sql" and delta.get("chart"):
                    yield _sse({"type": "chart", "option": delta["chart"]})
                if node == "prepare" and delta.get("citations"):
                    yield _sse({"type": "citations", "items": delta["citations"]})

        # ---- 流式生成最终回答（打字机效果） ----
        parts: list[str] = []
        canned = final_state.get("canned_answer")
        if canned:
            answer = canned
            yield _sse({"type": "answer", "text": answer})
        else:
            try:
                for delta in chat_stream(final_state["reply_messages"], temperature=0.2):
                    parts.append(delta)
                    yield _sse({"type": "answer_delta", "text": delta})
                answer = "".join(parts)
            except Exception as exc:  # noqa: BLE001 限流/断流兜底：已收到的部分照常展示
                log.error("answer stream failed: %r", exc)
                answer = "".join(parts) or "（模型服务暂时不可用，请稍后再试）"
            # 全量兜底事件：前端即使丢了几个增量帧也能用全文覆盖
            yield _sse({"type": "answer", "text": answer})

        # ---- 助手回复落库（meta 存 SQL/图表/引用，历史回看可完整还原） ----
        with Session(get_session_factory().kw["bind"]) as db:
            db.add(ChatMessage(
                session_id=session_id, role="assistant",
                content=answer,
                meta={"sql": final_state.get("sql_result"),
                      "chart": final_state.get("chart"),
                      "citations": final_state.get("citations")},
            ))
            db.commit()

        yield _sse({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/chat/sessions")
def list_sessions():
    """历史会话列表（最新的在前，前端下拉切换用）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        rows = session.execute(
            select(ChatSession).order_by(ChatSession.id.desc()).limit(20)
        ).scalars().all()
        return [{
            "id": r.id,
            "title": r.title,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
        } for r in rows]


@router.get("/chat/history")
def chat_history(session_id: int = Query(...)):
    """某次会话的完整消息记录（含 meta，前端据此还原 SQL/表格/图表/引用）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        rows = session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
        ).scalars().all()
        return [{
            "role": r.role,
            "content": r.content,
            "meta": r.meta,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
        } for r in rows]


router.add_api_route("/chat/stream", chat_stream_api, methods=["POST"],
                     summary="智能体对话（SSE 流式）")
