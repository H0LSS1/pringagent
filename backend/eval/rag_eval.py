"""RAG 检索评测：命中率 + 平均延迟。

【评测口径】对每个用例执行真实检索（BM25+向量混合，与线上一致），
top-6 召回文本里包含预期关键词组即判命中。目标准确率 ≥85%（plan.md 验收线）。

用法（backend 目录下）：
    .venv\\Scripts\\python -m eval.rag_eval            # 全量评测
    .venv\\Scripts\\python -m eval.rag_eval --show     # 显示每例命中详情
"""

import argparse
import time

from eval.cases import RAG_CASES
from rag.retriever import get_retriever

TARGET_ACCURACY = 0.85
TOP_K = 6


def run(show: bool = False) -> float:
    retriever = get_retriever()
    print(f"[rag_eval] knowledge chunks={retriever.size}, cases={len(RAG_CASES)}, top_k={TOP_K}")

    passed = 0
    latencies: list[float] = []
    for i, case in enumerate(RAG_CASES, start=1):
        t0 = time.monotonic()
        hits = retriever.search(case["question"], top_k=TOP_K)
        elapsed = (time.monotonic() - t0) * 1000
        latencies.append(elapsed)
        joined = " ".join(h["content"] for h in hits)
        hit_sets = case["expect_any"]
        ok = any(all(kw in joined for kw in group) for group in hit_sets)
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case['question']}  ({elapsed:.0f}ms)")
        if show or not ok:
            for h in hits[:3]:
                print(f"       <- [{h['doc']}|{h['section']}] {h['content'][:50]}...")

    accuracy = passed / len(RAG_CASES)
    avg_ms = sum(latencies) / len(latencies)
    print(f"\n[rag_eval] accuracy = {passed}/{len(RAG_CASES)} = {accuracy:.0%} "
          f"(target >= {TARGET_ACCURACY:.0%}) | avg latency = {avg_ms:.0f}ms")
    return accuracy


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAG 检索评测")
    ap.add_argument("--show", action="store_true", help="显示每例检索详情")
    args = ap.parse_args()
    acc = run(show=args.show)
    raise SystemExit(0 if acc >= TARGET_ACCURACY else 1)
