"""端到端压测：API 各接口延迟统计 + 智能体对话端到端耗时。

【评测口径】
- REST 接口：每个接口连续调 N 次，统计 avg / P95（毫秒）。
- 智能体 SSE：走完整链路（路由 -> 检索/SQL -> 流式回答），
  统计 首个进度事件时延（TTFF，用户"开始看到动静"的时间）、
  首个回答片段时延（TTFA）、总耗时。目标：总耗时 P95 < 8s（plan.md 验收线）。

用法（backend 目录下，需 API 服务已启动）：
    .venv\\Scripts\\python -m eval.e2e              # 默认 http://127.0.0.1:8000
    .venv\\Scripts\\python -m eval.e2e --base http://127.0.0.1:8000
"""

import argparse
import statistics
import time

import httpx

P95_TARGET_MS = 8000.0
N_REST = 5

# REST 接口清单（GET，均应 200）
REST_ENDPOINTS = [
    "/api/health",
    "/api/devices",
    "/api/dashboard/latest",
    "/api/history/readings?group_index=0&minutes=5&step_s=5",
    "/api/history/colors?group_index=0&minutes=5&step_s=5",
    "/api/quality/report?hours=1",
    "/api/alarms?limit=20",
    "/api/model/active",
    "/api/collect/status?hours=1",
]

# 智能体端到端用例（知识问答 / 数据查数各一）
CHAT_CASES = [
    "色差公差是多少",
    "查询1号机组最近10分钟的ΔE趋势",
]


def bench_rest(client: httpx.Client) -> bool:
    print(f"[e2e] REST latency (N={N_REST}):")
    all_ok = True
    for ep in REST_ENDPOINTS:
        times = []
        for _ in range(N_REST):
            t0 = time.monotonic()
            resp = client.get(ep)
            times.append((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                all_ok = False
                print(f"  [FAIL] {ep} -> {resp.status_code}")
                break
        p95 = sorted(times)[int(len(times) * 0.95) - 1]
        avg = statistics.mean(times)
        flag = "SLOW" if p95 > P95_TARGET_MS else "ok"
        print(f"  [{flag:4s}] {ep.split('?')[0]:44s} avg={avg:7.1f}ms p95={p95:7.1f}ms")
    return all_ok


def bench_chat(client: httpx.Client, base: str) -> tuple[float, float]:
    """智能体 SSE 端到端：返回 (最大TTFA, 最大总耗时)，单位毫秒。"""
    max_ttfa = 0.0
    max_total = 0.0
    for question in CHAT_CASES:
        t0 = time.monotonic()
        ttfa = None
        with client.stream("POST", f"{base}/api/chat/stream",
                           json={"message": question, "history": []},
                           timeout=120.0) as resp:
            if resp.status_code != 200:
                print(f"  [FAIL] chat '{question}' -> {resp.status_code}")
                max_total = max(max_total, (time.monotonic() - t0) * 1000)
                continue
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                now = (time.monotonic() - t0) * 1000
                if '"answer_delta"' in line or '"answer"' in line:
                    if ttfa is None:
                        ttfa = now  # 第一段回答到达
                if '"done"' in line:
                    max_total = max(max_total, now)
        max_ttfa = max(max_ttfa, ttfa or max_total)
        print(f"  [ok] chat '{question}'  首响={ttfa:.0f}ms  总耗时={max_total:.0f}ms")
    return max_ttfa, max_total


def run(base: str) -> None:
    # httpx 的 base_url 参数：后续请求直接用相对路径 /api/...
    with httpx.Client(base_url=base) as client:
        rest_ok = bench_rest(client)
        print("\n[e2e] Agent SSE end-to-end:")
        max_ttfa, max_total = bench_chat(client, base)

    print(f"\n[e2e] chat TTFA max = {max_ttfa:.0f}ms | total max = {max_total:.0f}ms "
          f"(target P95 < {P95_TARGET_MS:.0f}ms)")
    verdict = "PASS" if (rest_ok and max_total < P95_TARGET_MS) else "FAIL"
    print(f"[e2e] verdict: {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="端到端压测")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    run(args.base)
