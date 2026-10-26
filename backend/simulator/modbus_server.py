"""模拟印刷机 PLC + 在线分光光度计 的 Modbus TCP 服务端。

- unit 1 = PLC：车速/温湿度 + 每机组 4 压力模块（实际/初始/设定，可写设定值）
- unit 2 = 分光光度计：每机组标准 Lab / 实测 Lab / ΔE / ΔD / 密度
- 设定压力寄存器可被采集端反向写入（双向通信）：实际压力按一阶惯性向设定值收敛
- 启动：.venv\\Scripts\\python -m simulator.modbus_server  （在 backend 目录下）
"""

import argparse
import random
import threading
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer

from config import settings
from register_map import (
    COLOR_SCALE,
    PLC_BLOCK_SIZE,
    PLC_P_ACTUAL_OFF,
    PLC_P_SET_OFF,
    PLC_P_INIT_OFF,
    PLC_REG_GROUP_COUNT,
    PLC_REG_HEARTBEAT,
    PLC_REG_HUMIDITY,
    PLC_REG_MACHINE_STATUS,
    PLC_REG_SPEED,
    PLC_REG_TEMPERATURE,
    PLC_ROLLER_TEMP_OFF,
    PLC_UNIT_ID,
    PRESSURE_SCALE,
    SPECTRO_BLOCK_SIZE,
    SPECTRO_REG_GROUP_COUNT,
    SPECTRO_REG_MEASURE_SEQ,
    SPECTRO_REG_STATUS,
    SPECTRO_UNIT_ID,
    SPEED_SCALE,
    SP_DENSITY_OFF,
    SP_DELTA_D_OFF,
    SP_DELTA_E_OFF,
    SP_L_MEAS_OFF,
    SP_L_STD_OFF,
    SP_SEQ_OFF,
    SP_STATUS_OFF,
    TEMP_SCALE,
    from_i16,
    plc_group_base,
    spectro_group_base,
    to_i16,
)
from simulator.physics import (
    CALIB_OFFSET_RANGE,
    DE_TOLERANCE,
    measure_color,
    optimal_pressure,
)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class ZeroBasedSlaveContext(ModbusSlaveContext):
    """去掉协议侧 address+1 偏移，使寄存器地址与 register_map 的 0 基布局一致。"""

    def getValues(self, fc_as_hex, address, count=1):  # noqa: N802 (pymodbus 命名)
        return self.store[self.decode(fc_as_hex)].getValues(address, count)

    def setValues(self, fc_as_hex, address, values):  # noqa: N802 (pymodbus 命名)
        self.store[self.decode(fc_as_hex)].setValues(address, values)


class PressState:
    """印刷车间状态：车速恒定、压力设定值漂移+阶跃扰动、实际值一阶跟随。"""

    def __init__(self, n_groups: int, seed: int, speed: float = 150.0):
        self.rng = random.Random(seed)
        self.n = n_groups
        # 车速固定在 60~200 m/min（演示口径：稳定运行在一个值，不随机游走）
        self.speed = clamp(speed, 60.0, 200.0)
        self.temp = 24.5     # ℃
        self.hum = 55.0      # %RH
        self.heartbeat = 0
        self.measure_seq = 0
        # 每机组固定机械标定偏移：初始压力 = 启动时的颜色最优压力 + 标定偏移，
        # 此后固定不变（人工调试基准，仅供复位；它不随机器状态慢漂，
        # 所以运行久了"初始压力"未必还是颜色最优——这正是模型存在的意义）
        self.calib = [self.rng.uniform(-CALIB_OFFSET_RANGE, CALIB_OFFSET_RANGE)
                      for _ in range(n_groups)]
        # 磨损/油墨批次慢漂项：颜色最优压力 = 初始压力公式 + 磨损量（随时间随机游走）
        self.wear = [[0.0] * 4 for _ in range(n_groups)]
        # 设定值从"当前颜色最优"出发（开机即色准）
        self.setpoint = [[self.color_optimum(g, z) + self.rng.uniform(-0.2, 0.2)
                          for z in range(4)] for g in range(n_groups)]
        self.actual = [[sp + self.rng.uniform(-0.35, 0.35) for sp in row]
                       for row in self.setpoint]
        self.roller_temp = [36.0 + g * 0.8 for g in range(n_groups)]

    def init_pressure(self, g: int, z: int) -> float:
        """初始压力（人工调试基准）：固定不变，仅用于复位，不保证颜色达标。"""
        return optimal_pressure(self.speed, g, z) + self.calib[g]

    def color_optimum(self, g: int, z: int) -> float:
        """当前颜色最优压力：初始压力公式 + 慢漂磨损量（ΔE 对它最小）。"""
        return optimal_pressure(self.speed, g, z) + self.calib[g] + self.wear[g][z]

    def step(self) -> None:
        rng = self.rng
        # 车速恒定不变（稳定运行口径）；温湿度保留小幅波动
        self.temp = clamp(self.temp + rng.gauss(0.0, 0.05), 18.0, 30.0)
        self.hum = clamp(self.hum + rng.gauss(0.0, 0.35), 30.0, 80.0)
        self.heartbeat += 1
        for g in range(self.n):
            for z in range(4):
                # 磨损慢漂：颜色最优压力随时间缓慢移动（印版磨损/油墨批次/环境变化）
                self.wear[g][z] = clamp(self.wear[g][z] + rng.gauss(0.0, 0.002), -0.4, 0.4)
                # 设定值缓慢漂移；小概率阶跃扰动（模拟辊磨损/机械松动等故障）
                # 压力量程 -5~5 kN/m：设定值限制在 [-4.5, 4.5]
                self.setpoint[g][z] += rng.gauss(0.0, 0.015)
                if rng.random() < 0.0008:
                    self.setpoint[g][z] += rng.choice((-1.0, 1.0)) * rng.uniform(0.5, 1.3)
                self.setpoint[g][z] = clamp(self.setpoint[g][z], -4.5, 4.5)
                # 实际值一阶惯性向设定值收敛 + 测量噪声（限幅 [-4.8, 4.8]）
                self.actual[g][z] += (self.setpoint[g][z] - self.actual[g][z]) * 0.25
                self.actual[g][z] += rng.gauss(0.0, 0.03)
                self.actual[g][z] = clamp(self.actual[g][z], -4.8, 4.8)
            self.roller_temp[g] = clamp(self.roller_temp[g] + rng.gauss(0.0, 0.1), 25.0, 60.0)

    def group_dev(self, g: int) -> float:
        """机组内 4 个压力模块 |实际-颜色最优| 均值（kN/m）——色差的物理驱动。

        关键：ΔE 由"实际压力偏离颜色最优"驱动，而不是偏离初始压力——
        初始压力只是机械基准，颜色是否达标由分光光度计说了算。
        """
        return sum(abs(self.actual[g][z] - self.color_optimum(g, z)) for z in range(4)) / 4.0

    def next_measurements(self):
        """推进测量序号并返回每机组颜色测量结果。"""
        self.measure_seq += 1
        rows = []
        for g in range(self.n):
            rows.append((g, *measure_color(g, self.group_dev(g), self.rng)))
        return rows

    def adopt_written_setpoints(self, plc_block) -> int:
        """吸收控制端对设定压力寄存器的写入（客户端写入优先于内部漂移）。"""
        adopted = 0
        for g in range(self.n):
            base = plc_group_base(g)
            cur = plc_block.getValues(base + PLC_P_SET_OFF, 4)
            if not cur:
                continue
            for z in range(4):
                written = from_i16(cur[z]) / PRESSURE_SCALE
                # 控制端写入的设定值可为负（量程 -5~5），超范围视为无效
                if -4.9 < written < 4.9 and abs(written - self.setpoint[g][z]) > 0.005:
                    self.setpoint[g][z] = clamp(written, -4.5, 4.5)
                    adopted += 1
        return adopted


def build_snapshot(state: PressState, plc_block) -> list[int]:
    vals = [0] * PLC_BLOCK_SIZE
    vals[PLC_REG_SPEED] = int(state.speed * SPEED_SCALE)
    vals[PLC_REG_HUMIDITY] = int(state.hum * TEMP_SCALE)
    vals[PLC_REG_TEMPERATURE] = to_i16(int(state.temp * TEMP_SCALE))
    vals[PLC_REG_GROUP_COUNT] = state.n
    vals[PLC_REG_MACHINE_STATUS] = 1
    vals[PLC_REG_HEARTBEAT] = state.heartbeat & 0xFFFF
    for g in range(state.n):
        base = plc_group_base(g)
        for z in range(4):
            # 压力可为负：寄存器按 int16 补码编码（to_i16 负数转 0~65535）
            vals[base + PLC_P_ACTUAL_OFF + z] = to_i16(int(state.actual[g][z] * PRESSURE_SCALE))
            vals[base + PLC_P_INIT_OFF + z] = to_i16(int(state.init_pressure(g, z) * PRESSURE_SCALE))
            vals[base + PLC_P_SET_OFF + z] = to_i16(int(state.setpoint[g][z] * PRESSURE_SCALE))
        vals[base + PLC_ROLLER_TEMP_OFF] = to_i16(int(state.roller_temp[g] * TEMP_SCALE))
        de = measure_color(g, state.group_dev(g), state.rng)[2]
        vals[base + 13] = 1 if de <= DE_TOLERANCE else 2  # 状态字: 1 合格 / 2 超差
    return vals


def build_spectro_snapshot(state: PressState, measurements) -> list[int]:
    vals = [0] * SPECTRO_BLOCK_SIZE
    vals[SPECTRO_REG_GROUP_COUNT] = state.n
    vals[SPECTRO_REG_MEASURE_SEQ] = state.measure_seq & 0xFFFF
    vals[SPECTRO_REG_STATUS] = 1
    for row in measurements:
        g, std_lab, meas_lab, de, dd, _d_std, d_meas = row
        base = spectro_group_base(g)
        for k in range(3):
            vals[base + SP_L_STD_OFF + k] = to_i16(int(std_lab[k] * COLOR_SCALE))
            vals[base + SP_L_MEAS_OFF + k] = to_i16(int(meas_lab[k] * COLOR_SCALE))
        vals[base + SP_DELTA_E_OFF] = int(de * COLOR_SCALE)
        vals[base + SP_DELTA_D_OFF] = to_i16(int(dd * COLOR_SCALE))
        vals[base + SP_DENSITY_OFF] = int(d_meas * COLOR_SCALE)
        vals[base + SP_STATUS_OFF] = 1 if de <= DE_TOLERANCE else 2
        vals[base + SP_SEQ_OFF] = state.measure_seq & 0xFFFF
    return vals


def updater_loop(state: PressState, plc_block, sp_block, interval: float) -> None:
    while True:
        try:
            state.adopt_written_setpoints(plc_block)
            state.step()
            measurements = state.next_measurements()
            plc_block.setValues(0, build_snapshot(state, plc_block))
            sp_block.setValues(0, build_spectro_snapshot(state, measurements))
        except Exception as exc:  # noqa: BLE001 模拟器线程不允许退出
            print(f"[simulator] updater error: {exc!r}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="PrintAgent Modbus TCP 模拟器（PLC + 分光光度计）")
    ap.add_argument("--host", default=settings.modbus_host)
    ap.add_argument("--port", type=int, default=settings.modbus_port)
    ap.add_argument("--groups", type=int, default=10, help="印刷机组数量（8-12）")
    ap.add_argument("--speed", type=float, default=150.0,
                    help="固定车速 m/min（60-200，默认150）")
    ap.add_argument("--interval", type=float, default=1.0, help="数据刷新周期（秒）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    zero = ModbusSequentialDataBlock(0, [0] * 16)
    plc_block = ModbusSequentialDataBlock(0, [0] * PLC_BLOCK_SIZE)
    sp_block = ModbusSequentialDataBlock(0, [0] * SPECTRO_BLOCK_SIZE)
    devices = {
        PLC_UNIT_ID: ZeroBasedSlaveContext(di=zero, co=zero, hr=plc_block, ir=plc_block),
        SPECTRO_UNIT_ID: ZeroBasedSlaveContext(di=zero, co=zero, hr=sp_block, ir=sp_block),
    }
    context = ModbusServerContext(slaves=devices, single=False)

    state = PressState(args.groups, args.seed, speed=args.speed)
    threading.Thread(target=updater_loop, args=(state, plc_block, sp_block, args.interval),
                     daemon=True).start()

    print(f"[simulator] Modbus TCP {args.host}:{args.port} | "
          f"unit{PLC_UNIT_ID}=PLC unit{SPECTRO_UNIT_ID}=分光光度计 | "
          f"{args.groups} 机组 x 4 压力模块 | 车速固定 {state.speed:.0f} m/min | "
          f"刷新 {args.interval}s", flush=True)
    StartTcpServer(context, address=(args.host, args.port))


if __name__ == "__main__":
    main()
