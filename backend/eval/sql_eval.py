"""Text2SQL 评测：执行成功率 + 平均尝试次数 + 延迟。

【评测口径】每个用例真实走一遍完整链路（LLM 生成 SQL -> sqlglot 沙箱校验
-> 执行 -> 失败自动修正重试），判定：
  ① 执行成功（sql_result.ok）
  ② 用对表：SQL 文本包含预期表名（must_tables_any 任一组全部出现）
  ③ 结果行数达标（min_rows）
三项全过才算 PASS。simple / complex 分档统计（目标：总体 ≥90%，复杂 ≥85%）。

用法（backend 目录下）：
    .venv\\Scripts\\python -m eval.sql_eval
"""

import argparse
import re
import time

from eval.cases import SQL_CASES
from agent.sql_tool import run_text2sql

TARGET_OVERALL = 0.90
TARGET_COMPLEX = 0.85


def table_hit(sql: str, must_tables_any: list[list[str]]) -> bool:
    """预期表命中判定：任一组关键词里出现任意一张表即通过。

    例：must_tables_any=[["alarms", "color_measurements"]] 表示
    用告警表或色彩表回答都算对（同一问题可有多条等价路径）。
    """
    lowered = sql.lower()
    return any(any(t in lowered for t in group) for group in must_tables_any)


def run() -> tuple[float, float]:
    stats = {"simple": [0, 0], "complex": [0, 0]}  # kind -> [passed, total]
    latencies: list[float] = []
    attempts_sum = 0

    for i, case in enumerate(SQL_CASES, start=1):
        t0 = time.monotonic()
        res = run_text2sql(case["question"])
        elapsed = (time.monotonic() - t0) * 1000
        latencies.append(elapsed)
        attempts_sum += res["attempts"]

        ok = (res["ok"]
              and table_hit(res["sql"], case["must_tables_any"])
              and len(res["rows"]) >= case["min_rows"])
        kind = case["kind"]
        stats[kind][1] += 1
        stats[kind][0] += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] ({kind:7s}) {case['question']}  "
              f"rows={len(res['rows'])} attempts={res['attempts']} ({elapsed:.0f}ms)")
        if not ok:
            print(f"       sql: {res['sql'][:120]}")
            if res["error"]:
                print(f"       error: {str(res['error'])[:120]}")

    simple_acc = stats["simple"][0] / max(stats["simple"][1], 1)
    complex_acc = stats["complex"][0] / max(stats["complex"][1], 1)
    total_passed = stats["simple"][0] + stats["complex"][0]
    total = stats["simple"][1] + stats["complex"][1]
    overall = total_passed / max(total, 1)
    avg_ms = sum(latencies) / len(latencies)
    avg_att = attempts_sum / max(total, 1)
    print(f"\n[sql_eval] overall = {total_passed}/{total} = {overall:.0%} "
          f"(target >= {TARGET_OVERALL:.0%})")
    print(f"[sql_eval] simple = {simple_acc:.0%} | complex = {complex_acc:.0%} "
          f"(target >= {TARGET_COMPLEX:.0%})")
    print(f"[sql_eval] avg latency = {avg_ms:.0f}ms | avg attempts = {avg_att:.2f}")
    return overall, complex_acc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Text2SQL 评测")
    args = ap.parse_args()
    overall, complex_acc = run()
    ok = overall >= TARGET_OVERALL and complex_acc >= TARGET_COMPLEX
    raise SystemExit(0 if ok else 1)
