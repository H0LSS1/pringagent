"""历史数据回填：按物理模型批量生成历史时序数据（演示/评测用）。

【零基础说明：为什么需要回填？】
实时采集 2 秒一条，攒 11 小时才有 2 万行——演示"百万级数据查询"和
训练模型不能干等。backfill 按 simulator.physics 同一套物理公式直接生成
历史数据写进 MySQL，几秒钟造出几十万行，且数据特征与实时数据完全一致。

【与实时采集的关系】
- 数据口径相同（共用 physics.py），模型训练时混着用没有问题
- 时间窗不重叠：end_ts 固定留 60 秒边距，保证"最新一行"永远是实时采集的
  （否则仪表盘/闭环调节可能查到一条"未来历史数据"，导致用错车速）

用法（backend 目录下）：
    .venv\\Scripts\\python -m collector.backfill --cycles 20000     # 20 万行/表（约 30s）
    .venv\\Scripts\\python -m collector.backfill --cycles 100000    # 百万级
"""

import argparse
import math
import random
import time
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from config import settings
from db.models import ColorMeasurement, PlcReading
from db.mysql import get_session_factory, init_schema
from simulator.physics import CALIB_OFFSET_RANGE, DE_TOLERANCE, measure_color, optimal_pressure


def generate(cycles: int, n_groups: int, interval_s: float, seed: int,
             chunk_rows: int, margin_s: float = 60.0) -> None:
    """主生成循环。

    cycles：周期数（每周期每机组各产生 1 行 PLC + 1 行颜色数据）
    margin_s：结束时间向前留的安全边距（秒），避免覆盖实时采集窗口
    """
    rng = random.Random(seed)
    # 每机组固定标定偏移（与模拟器同款设定：回归模型学不到的"机械差异"）
    calib = [rng.uniform(-CALIB_OFFSET_RANGE, CALIB_OFFSET_RANGE) for _ in range(n_groups)]
    # 磨损慢漂项：颜色最优压力随时间缓慢移动（与实时模拟器同款机制）
    wear = [[0.0] * 4 for _ in range(n_groups)]
    # 车速与实时模拟器口径一致：固定 150 m/min（60~200 稳定运行）
    speed = 150.0
    temp = 24.5
    hum = 55.0
    roller = [36.0 + g * 0.8 for g in range(n_groups)]
    # 结束时间留出安全边距，避免与实时采集窗口重叠
    end_ts = datetime.now().replace(microsecond=0) - timedelta(seconds=margin_s)
    start_ts = end_ts - timedelta(seconds=cycles * interval_s)

    plc_chunk: list[dict] = []
    color_chunk: list[dict] = []
    total = cycles * n_groups
    t0 = time.monotonic()
    done = 0

    with Session(get_session_factory().kw["bind"]) as session:
        for c in range(cycles):
            ts = start_ts + timedelta(seconds=c * interval_s)
            # 车速恒定；温湿度小幅波动（与实时模拟器口径一致）
            temp = min(30.0, max(18.0, temp + rng.gauss(0.0, 0.05)))
            hum = min(80.0, max(30.0, hum + rng.gauss(0.0, 0.35)))
            for g in range(n_groups):
                # 初始压力（人工调试基准，固定不变）与 颜色最优压力（含慢漂磨损量）。
                # 色差由"实际 偏离 颜色最优"驱动——初始压力印出来的颜色不一定达标。
                p_init = [optimal_pressure(speed, g, z) + calib[g] for z in range(4)]
                for z in range(4):
                    wear[g][z] = max(-0.4, min(0.4, wear[g][z] + rng.gauss(0.0, 0.002)))
                p_opt = [p_init[z] + wear[g][z] for z in range(4)]
                # 压力偏差：均值 ~0.7、部分样本超差 -> 触发告警/调节场景
                # （量程 -5~5：实际值限幅 [-4.8, 4.8]）
                signed = [rng.gauss(rng.choice((-1.0, 1.0)) * 0.7, 0.5) for _ in range(4)]
                p_act = [max(-4.8, min(4.8, p_opt[z] + signed[z])) for z in range(4)]
                dev = sum(abs(p_act[z] - p_opt[z]) for z in range(4)) / 4.0
                roller[g] = min(60.0, max(25.0, roller[g] + rng.gauss(0.0, 0.1)))
                p_set = [max(-4.8, min(4.8, p_act[z] - rng.uniform(0.0, 0.25))) for z in range(4)]

                plc_chunk.append({
                    "ts": ts, "group_index": g,
                    "speed_mmin": round(speed, 1), "humidity": round(hum, 1),
                    "temperature": round(temp, 1), "roller_temp": round(roller[g], 1),
                    "p_actual_1": round(p_act[0], 2), "p_actual_2": round(p_act[1], 2),
                    "p_actual_3": round(p_act[2], 2), "p_actual_4": round(p_act[3], 2),
                    "p_init_1": round(p_init[0], 2), "p_init_2": round(p_init[1], 2),
                    "p_init_3": round(p_init[2], 2), "p_init_4": round(p_init[3], 2),
                    "p_set_1": round(p_set[0], 2), "p_set_2": round(p_set[1], 2),
                    "p_set_3": round(p_set[2], 2), "p_set_4": round(p_set[3], 2),
                })

                # 颜色由压力偏差按物理公式算出（与实时链路同源，见 physics.measure_color）
                _std_lab, meas, de, dd, d_std, d_meas = measure_color(g, dev, rng)
                color_chunk.append({
                    "ts": ts, "group_index": g,
                    "l_std": round(_std_lab[0], 2), "a_std": round(_std_lab[1], 2),
                    "b_std": round(_std_lab[2], 2),
                    "l_meas": round(meas[0], 2), "a_meas": round(meas[1], 2),
                    "b_meas": round(meas[2], 2),
                    "delta_e": round(de, 2), "delta_d": round(dd, 2),
                    "density_std": d_std, "density_meas": d_meas,
                    "in_tolerance": de <= DE_TOLERANCE,
                    "measure_seq": c + 1,
                })

            # 攒够一批（默认 5000 行）就落库一次，避免内存和事务过大
            if len(plc_chunk) >= chunk_rows:
                session.execute(insert(PlcReading), plc_chunk)
                session.execute(insert(ColorMeasurement), color_chunk)
                session.commit()
                plc_chunk.clear()
                color_chunk.clear()

            done += n_groups
            if c % 2000 == 0 and c:
                rate = done / max(time.monotonic() - t0, 1e-6)
                print(f"  backfill {c + 1}/{cycles} cycles, {done}/{total} rows, "
                      f"{rate:.0f} rows/s", flush=True)

        if plc_chunk:
            session.execute(insert(PlcReading), plc_chunk)
            session.execute(insert(ColorMeasurement), color_chunk)
            session.commit()

    elapsed = time.monotonic() - t0
    with Session(get_session_factory().kw["bind"]) as session:
        n_plc = session.execute(select(func.count()).select_from(PlcReading)).scalar()
        n_color = session.execute(select(func.count()).select_from(ColorMeasurement)).scalar()
    print(f"[backfill] done in {elapsed:.1f}s | plc_readings={n_plc} "
          f"color_measurements={n_color} | span="
          f"{(end_ts - start_ts) / timedelta(hours=1):.1f}h @{interval_s}s/cycle")


def main() -> None:
    ap = argparse.ArgumentParser(description="PrintAgent 历史数据回填")
    ap.add_argument("--cycles", type=int, default=20000, help="周期数（每周期每机组一行）")
    ap.add_argument("--groups", type=int, default=10)
    ap.add_argument("--interval-s", type=float, default=5.0, help="模拟采样间隔（与实时采集一致）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--batch-rows", type=int, default=5000)
    args = ap.parse_args()

    init_schema()  # 幂等：表不存在时先建
    generate(args.cycles, args.groups, args.interval_s, args.seed, args.batch_rows)


if __name__ == "__main__":
    main()
