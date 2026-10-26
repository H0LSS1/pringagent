"""印刷工艺智能体：LangGraph 状态机工作流（阶段三核心）。

【零基础说明：什么是 Agent / LangGraph？】
- 智能体（Agent）= 大模型 + 工具 + 工作流。大模型本身只会"说话"，
  不会查数据库、不会检索文档；我们把"知识库检索""SQL 查询"封装成工具，
  让模型在合适的时机调用它们。
- LangGraph 是 LangChain 团队的"状态机"框架：把智能体的处理过程画成一张
  图（graph），每个节点（node）做一步工作，边（edge）决定下一步去哪。
  好处：流程可控、可观测（每个节点都能流式输出进度）、可回放。

【我们的工作流】
                     ┌──────────────┐
        START ──────>│  route 意图路由 │（规则优先，LLM 兜底）
                     └──┬────┬────┬──┘
          知识类问题 ↓    ↓ 数据查数   ↓ 闲聊
           ┌──────────┐ ┌──────────┐ ┌──────────┐
           │ knowledge │ │   sql    │ │ chitchat │
           │ 混合检索  │ │ Text2SQL │ │ 直接回复  │
           └────┬─────┘ └────┬─────┘ └────┬─────┘
                │   沙箱执行+重试  │            │
                └───────┬────────┘            │
                     ┌──┴─────────┐           │
                     │  prepare   │           │
                     │ 归因解读+引用 │           │
                     └──────┬─────┘           │
                          END <───────────────┘

【状态（State）】
图上流动的"文件夹"：每个节点读它、往里加自己的结果，下一个节点接着用。
"""

import json
import logging
import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.sql_tool import build_chart, run_text2sql
# 注意：最终回答的 LLM 调用已移至 SSE 层（app/routes_chat.py）流式执行，图内不再直接调用
from rag.retriever import get_retriever

log = logging.getLogger("agent")


class AgentState(TypedDict):
    """整个工作流共享的状态（节点间传递的"文件夹"）。"""
    question: str                       # 用户本轮的问题
    history: list[dict]                 # 多轮对话历史 [{"role","content"}]
    intent: str                         # 路由结果: knowledge / sql / chitchat
    retrieved: list[dict]               # 知识检索结果（citations 的来源）
    sql_result: dict                    # Text2SQL 完整结果
    reply_messages: list[dict]          # 最终回答的 LLM 提示词（由 SSE 层流式调用）
    canned_answer: str | None           # 无需 LLM 的固定回复（如查询失败提示）
    citations: list[dict]               # 引用溯源（doc + section）
    chart: dict | None                  # ECharts 图表配置（查数时可能有）


# ================= 节点 1：意图路由 =================

# 规则能明确判断的就不问 LLM（省时间、稳定）
# 查数信号：显式查询动词 / 聚合词（"超标"单独出现不算——"超标是什么原因"是知识问题）
_SQL_HINTS = re.compile(r"查[一询看]?[下询]?|统计|列出|多少条|几条|报表|趋势图|"
                        r"平均|最高|最低|最差|最优|多少|趋势|历史数据")
# 知识信号：因果/概念/方法类问法（优先级高于聚合词：含"为什么/原因"时归知识）
_KNOWLEDGE_HINTS = re.compile(r"为什么|什么原因|什么问题|是什么|含义|概念|区别|标准|规范|"
                              r"公差|原理|怎么调|如何调|建议|排查|原因|ISO\s?12647|G7")


def _route(question: str) -> str:
    """意图路由：规则优先，模糊时让 LLM 分类。返回 'sql' | 'knowledge' | 'chitchat'。"""
    has_knowledge = bool(_KNOWLEDGE_HINTS.search(question))
    has_query = bool(_SQL_HINTS.search(question))
    if has_knowledge and not has_query:
        return "knowledge"
    if has_query:
        return "sql"
    if has_knowledge:
        return "knowledge"
    # 规则拿不准 -> LLM 结构化分类（json_mode 强制输出 JSON）
    prompt = (
        "判断用户意图，只输出 JSON：{\"intent\": \"sql\"|\"knowledge\"|\"chitchat\"}\n"
        "sql=要查询产线数据（历史记录/统计/数值）；"
        "knowledge=问印刷知识/标准/工艺/故障原因；"
        "chitchat=打招呼或与印刷无关。\n"
        f"用户输入：{question}"
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], temperature=0.0,
                   max_tokens=50, json_mode=True)
        intent = json.loads(raw).get("intent", "chitchat")
        return intent if intent in ("sql", "knowledge", "chitchat") else "chitchat"
    except Exception as exc:  # noqa: BLE001 LLM 挂了默认走知识检索
        log.warning("route fallback: %r", exc)
        return "knowledge"


def route_node(state: AgentState) -> dict:
    """节点：意图路由。只往状态里写 intent 字段。"""
    intent = _route(state["question"])
    log.info("route -> %s", intent)
    return {"intent": intent}


# ================= 节点 2：知识检索 =================

def knowledge_node(state: AgentState) -> dict:
    """节点：BM25+向量混合检索印刷知识库，把命中的知识块放进状态。"""
    hits = get_retriever().search(state["question"], top_k=6)
    log.info("knowledge hits=%d", len(hits))
    return {"retrieved": hits}


# ================= 节点 3：Text2SQL =================

def sql_node(state: AgentState) -> dict:
    """节点：自然语言查产线数据库（生成 SQL -> 沙箱校验 -> 执行 -> 自动重试）。"""
    result = run_text2sql(state["question"], state.get("history"))
    chart = build_chart(result["columns"], result["rows"]) if result["ok"] else None
    log.info("sql ok=%s attempts=%s", result["ok"], result["attempts"])
    return {"sql_result": result, "chart": chart}


# ================= 节点 4：准备最终回答（LLM 调用交给 SSE 层流式执行） =================

_PERSONA = (
    "你是印刷工艺智能体，服务于一台 10 色组胶印机产线（每组 4 个压力模块），"
    "掌握印刷标准知识（ISO 12647 / G7 / GB/T 系列）并能查询产线数据库"
    "（压力/色差ΔE/密度差ΔD/车速/温湿度历史）。回答要专业、简洁、给出可执行建议。"
)


def prepare_node(state: AgentState) -> dict:
    """节点：把工具结果"翻译"成最终回答的提示词。

    【为什么 LLM 调用不放在图里？】最终回答需要流式输出（打字机效果），
    LangGraph 节点只能返回最终状态、无法逐 token 往外推。
    所以本节点只组装提示词（reply_messages），由 SSE 层用 llm.chat_stream
    流式生成——节点事件（检索完成/SQL 就绪）依然实时推送，进度可见。
    """
    intent = state["intent"]
    question = state["question"]

    if intent == "knowledge" and state["retrieved"]:
        # 把知识块编号 [1][2]... 放进提示词，要求答案里用同编号引用
        context = "\n\n".join(
            f"[{i + 1}]（{c['doc']}·{c['section']}）{c['content']}"
            for i, c in enumerate(state["retrieved"]))
        messages = [
            {"role": "system", "content": _PERSONA},
            {"role": "user", "content":
                f"请仅依据以下知识块回答问题。回答末尾列出引用的知识块编号。"
                f"知识块不够回答时明确说明信息不足，不要编造。\n\n"
                f"知识块：\n{context}\n\n问题：{question}"},
        ]
        citations = [{"doc": c["doc"], "section": c["section"]}
                     for c in state["retrieved"][:3]]
        return {"reply_messages": messages, "citations": citations}

    if intent == "sql":
        sr = state["sql_result"]
        if not sr["ok"]:
            canned = (f"查询执行失败：{sr['error']}。\n"
                      "可以换个问法（比如：查询1号机组最近24小时的ΔE记录），"
                      "或先让我解释相关工艺知识。")
            return {"canned_answer": canned}
        # 把结果表（最多 10 行）交给模型解读；完整表格由前端直接渲染
        preview_rows = sr["rows"][:10]
        table_text = "\n".join(
            " | ".join(str(v) for v in row) for row in preview_rows)
        messages = [
            {"role": "system", "content": _PERSONA},
            {"role": "user", "content":
                f"我执行了查询「{question}」，SQL 与结果如下。\n"
                f"SQL: {sr['sql']}\n{sr['note']}\n"
                f"列: {sr['columns']}\n数据（最多10行）：\n{table_text}\n\n"
                "请用中文总结数据反映的情况（趋势/异常/可能原因与建议），"
                "数字要准确引用，不要编造表里没有的数。"},
        ]
        return {"reply_messages": messages}

    # 闲聊/其他
    return {"reply_messages": [
        {"role": "system", "content": _PERSONA},
        {"role": "user", "content": question},
    ]}


# ================= 组装状态机 =================

def _route_edge(state: AgentState) -> Literal["knowledge", "sql", "chitchat"]:
    """条件边的走向判断：route 之后按 intent 去不同节点。"""
    return state["intent"]  # type: ignore[return-value]


def build_graph():
    """构建并编译工作流图（进程内只编译一次，之后反复调用）。"""
    g = StateGraph(AgentState)
    g.add_node("route", route_node)
    g.add_node("knowledge", knowledge_node)
    g.add_node("sql", sql_node)
    g.add_node("prepare", prepare_node)

    g.add_edge(START, "route")
    # 条件边：route 的结果决定下一步；knowledge/sql 之后统一进 interpret；
    # interpret 结束即结束（闲聊没有独立节点，路由结果里 chitchat 直接进 interpret）
    g.add_conditional_edges("route", _route_edge,
                            {"knowledge": "knowledge", "sql": "sql",
                             "chitchat": "prepare"})
    g.add_edge("knowledge", "prepare")
    g.add_edge("sql", "prepare")
    g.add_edge("prepare", END)
    return g.compile()


# 模块级单例：import 时编译一次
graph = build_graph()
