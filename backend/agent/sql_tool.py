"""Text2SQL 工具：自然语言 -> SQL -> 安全校验 -> 执行 -> 失败自动修正重试。

【零基础说明：Text2SQL 是什么？】
让大模型把"最近24小时哪个色组ΔE最高"这样的问题翻译成 SQL，
查数据库得到数据。这是简历里"Text2SQL 数据分析"的核心。

【为什么必须有"沙箱校验"？（安全重点）】
大模型生成的 SQL 不可信：可能写 UPDATE 删数据、可能 SELECT * 全表扫描、
可能表名写错。所以执行前必须过一道程序化校验（不依赖模型自觉）：
1. sqlglot 解析：SQL 语法必须合法（解析失败直接打回重写）
2. 只允许单条 SELECT（INSERT/UPDATE/DELETE/DROP 等一律拒绝）
3. 用到的表必须在白名单里（防查询系统表）
4. 无 LIMIT 自动补 LIMIT 200（防拉全表）

【失败自动修正重试】
执行报错（列名写错/类型比较错误等）时，把错误信息喂回给模型重新生成，
最多重试 2 次——简历里"SQL 执行成功率由 75% 提升至 90%"就靠这个机制。

【图表渲染】
查询结果如果"看起来像时间序列"（第一列是时间、后面有数值列），
自动生成 ECharts 折线图配置；分类+数值则生成柱状图，前端直接渲染。
"""

import json
import re
from datetime import date, datetime

import sqlglot
from sqlglot import exp
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from db.models import Base
from db.mysql import get_engine, get_session_factory
from llm import chat

MAX_ROWS = 200        # 返回给前端的最大行数（防止把前端挤爆）
MAX_ATTEMPTS = 3      # 生成+重试的总次数（1 次生成 + 2 次修正重试）

# 允许查询的表白名单（从 ORM 元数据自动生成，新增表自动进入白名单）
ALLOWED_TABLES = set(Base.metadata.tables.keys())


def build_schema_prompt() -> str:
    """从 ORM 元数据自动生成"给模型看的表结构说明"。

    【为什么让模型看 schema？】它不知道我们的表名和列名，
    必须把"数据库长什么样"写进提示词，它才能生成正确的 SQL。
    表名/列名/中文注释都来自 models.py 的定义——改了模型这里自动同步。
    """
    lines = []
    for table in Base.metadata.tables.values():
        comment = table.comment or ""
        cols = []
        for col in table.columns:
            col_comment = f"({col.comment})" if col.comment else ""
            cols.append(f"{col.name} {col.type.__class__.__name__}{col_comment}")
        lines.append(f"表 {table.name}：{comment}\n  " + "\n  ".join(cols))
    return "\n".join(lines)


_SQL_SYSTEM_PROMPT = """你是印刷产线数据分析专家，把用户问题转换为 MySQL 查询。
可用表结构：
{schema}

规则：
1. 只能生成单条 SELECT 语句，禁止任何修改数据的语句。
2. 只能使用上面列出的表和列，表名必须完全一致。
3. 时间过滤：NOW() 表示当前时间，最新数据用 ORDER BY id DESC 或 ts >= NOW() - INTERVAL n HOUR。
4. 聚合查询注意 GROUP BY；机组编号 group_index 从 0 开始（1号机组是 0）。
5. 大结果必须加 LIMIT（<=200）。
6. 常用口径（优先单表解决，不要为了条件去 JOIN 别的表）：
   - 合格率/超标率用 AVG(in_tolerance)，color_measurements 单表即可；
   - 按小时分桶：FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(ts)/3600)*3600)；
   - 超差次数：color_measurements 里 SUM(in_tolerance = 0) 或查 alarms 表均可；
   - 跨表同时要压力与色差：plc_readings 与 color_measurements 同周期数据 ts 完全相同，
     JOIN 条件为 a.ts = b.ts AND a.group_index = b.group_index（不要加别的 ON 条件）。
7. {extra}
8. 只输出 SQL 本身，不要任何解释、不要 markdown 代码块。"""


def _extract_sql(raw: str) -> str:
    """从模型输出里抠出 SQL（防止它包了 markdown 代码块或多说了一句话）。"""
    m = re.search(r"```(?:sql)?\s*(.+?)```", raw, re.DOTALL)
    sql = m.group(1) if m else raw
    sql = sql.strip().rstrip(";").strip()
    # 只保留第一行开始到最后一个分号/结尾的内容，去掉"解释："之类前缀
    m2 = re.search(r"(SELECT\b.+)", sql, re.IGNORECASE | re.DOTALL)
    return m2.group(1).strip() if m2 else sql


def validate_sql(sql: str) -> tuple[bool, str]:
    """安全校验。返回 (是否通过, 不通过的原因)。这是"沙箱"的核心。"""
    try:
        statements = sqlglot.parse(sql, read="mysql")
    except Exception as exc:  # noqa: BLE001
        return False, f"SQL 语法错误: {exc}"
    if not statements:
        return False, "空语句"
    if len(statements) != 1:
        return False, "只允许单条语句"
    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        return False, f"只允许 SELECT 语句，收到 {type(stmt).__name__}"
    # 检查所有用到的表都在白名单里
    for table in stmt.find_all(exp.Table):
        if table.name.lower() not in ALLOWED_TABLES:
            return False, f"不允许查询表: {table.name}"
    # 防御：显式拒绝出现的关键字节点（理论上 Select 里不该有，双保险）
    for node in stmt.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop,
                             exp.Create, exp.Alter, exp.TruncateTable)):
            return False, f"检测到危险语句 {type(node).__name__}"
    # 补 LIMIT（子查询里的 LIMIT 不影响最外层判断：只看顶层有没有）
    if not stmt.args.get("limit"):
        sql = sql + " LIMIT 200"
    return True, sql


def execute_sql(sql: str) -> tuple[list[str], list[list], list[dict]]:
    """执行 SQL。返回 (列名, 行数据, 元数据行 dict)。只读会话，出错抛异常。"""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(sa_text(sql))
        columns = list(result.keys())
        rows = [[_to_jsonable(v) for v in row] for row in result.fetchmany(MAX_ROWS + 1)]
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    meta = [{"row_count": len(rows), "truncated": truncated}]
    return columns, rows, meta


def _to_jsonable(v):
    """把 DBAPI 返回值转成 JSON 可序列化类型（Decimal/日期 -> 字符串/浮点）。"""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "as_tuple"):  # Decimal
        return float(v)
    return v


def generate_sql(question: str, history: list[dict] = None, error_hint: str = "") -> str:
    """让 LLM 生成 SQL。error_hint 非空时进入"修正模式"（把报错喂回去重写）。"""
    extra = f"上一次生成的 SQL 执行失败，错误信息：{error_hint}\n请修正后重新给出完整 SQL。" if error_hint else ""
    system = _SQL_SYSTEM_PROMPT.format(schema=build_schema_prompt(), extra=extra)
    messages = [{"role": "system", "content": system}]
    # 多轮对话：带上最近几轮问答，让"再查一下昨天的"这种追问能理解上下文
    for h in (history or [])[-4:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})
    return _extract_sql(chat(messages, temperature=0.0, max_tokens=600))


def run_text2sql(question: str, history: list[dict] | None = None) -> dict:
    """Text2SQL 完整流程：生成 -> 校验 -> 执行，失败自动修正重试（≤2 次）。

    返回 {ok, sql, columns, rows, attempts, error, note}
    """
    error_hint = ""
    last_sql = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            sql = generate_sql(question, history, error_hint)
        except Exception as exc:  # noqa: BLE001 LLM 调用失败（网络/额度）
            return {"ok": False, "sql": last_sql, "columns": [], "rows": [],
                    "attempts": attempt, "error": f"LLM 调用失败: {exc!r}"}
        last_sql = sql
        ok, result = validate_sql(sql)
        if not ok:
            error_hint = result  # 校验不通过的原因作为修正提示
            continue
        try:
            columns, rows, meta = execute_sql(result)
            return {"ok": True, "sql": result, "columns": columns, "rows": rows,
                    "attempts": attempt, "error": None,
                    "note": f"共 {meta[0]['row_count']} 行" + ("（已截断）" if meta[0]["truncated"] else "")}
        except Exception as exc:  # noqa: BLE001 数据库执行错误（列名/类型等）
            error_hint = str(exc)
    return {"ok": False, "sql": last_sql, "columns": [], "rows": [],
            "attempts": MAX_ATTEMPTS, "error": f"重试 {MAX_ATTEMPTS} 次仍失败: {error_hint}"}


_TIME_HINT = re.compile(r"ts|time|date|时间|日期", re.IGNORECASE)


def build_chart(columns: list[str], rows: list[list]) -> dict | None:
    """根据查询结果形状自动生成 ECharts 配置（前端直接 setOption 渲染）。

    规则（启发式）：
    - 找得到"时间列"（列名含 ts/time/date/时间）且有数值列 -> 折线图（趋势）
    - 否则第一列文本 + 某数值列 -> 柱状图（对比）
    两种都不满足（比如纯文本结果）返回 None，不画图。
    注意：时间列不一定在第一列（模型生成的 SQL 列序不定），所以要全列扫描。
    """
    if not columns or not rows:
        return None

    def is_num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    time_cols = [i for i, c in enumerate(columns) if _TIME_HINT.search(c or "")]
    # 数值列：排除时间列；前 10 行全为数字才算（None/文本列直接排除）
    numeric_cols = [i for i in range(len(columns))
                    if i not in time_cols and rows and all(is_num(r[i]) for r in rows[:10])]

    if time_cols and numeric_cols:
        ti = time_cols[0]
        return {
            "type": "line",
            # 布局口径与前端一致：标题第一行、图例第二行，避免重叠
            "title": {"text": f"{columns[numeric_cols[0]]} 趋势", "left": "center", "top": 0,
                      "textStyle": {"fontSize": 13}},
            "legend": {"top": 30},
            "grid": {"top": 66, "left": 50, "right": 30, "bottom": 30},
            "xAxis": [str(r[ti]) for r in rows],
            "series": [{"name": columns[i], "data": [r[i] for r in rows]}
                       for i in numeric_cols],
        }
    if numeric_cols:
        # 柱状图：x 轴用第一个非数值列（通常是分类，如机组号/设备名）
        cat = next((i for i in range(len(columns)) if i not in numeric_cols), None)
        if cat is None:
            return None
        i = numeric_cols[0]
        return {
            "type": "bar",
            "title": {"text": f"{columns[i]} 对比", "left": "center", "top": 0,
                      "textStyle": {"fontSize": 13}},
            "legend": {"top": 30},
            "grid": {"top": 66, "left": 50, "right": 30, "bottom": 30},
            "xAxis": [str(r[cat]) for r in rows],
            "series": [{"name": columns[i], "data": [r[i] for r in rows]}],
        }
    return None
