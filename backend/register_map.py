"""Modbus TCP 寄存器地址映射表（整个系统的"数据字典"）。

【零基础说明】
- Modbus 协议里设备的数据都放在"寄存器"里，你可以把寄存器想象成一排
  编号的小格子：每个格子存一个 16 位整数（0~65535）。
- 读数据 = 客户端发请求"把 0 号格子开始的 170 个格子发给我"；
  写数据 = "把这几个数写到指定格子里"。
- 真实设备的手册里会有一张"寄存器地址表"（哪个地址是车速、哪个是压力），
  这个文件就是我们系统的地址表。

【为什么要做成代码共享文件】
模拟器（往格子里写数）和采集端（从格子里读数）是两个独立进程，
如果各自记一套地址，改一处忘另一处就会读错数据。
所以布局常量只定义在这里，两边一起 import，保证永远一致。

【小知识】
- unit id：一台 Modbus 服务器上可以挂多个"设备"，用编号区分。
  我们用 1 号=印刷机 PLC，2 号=分光光度计（测颜色的仪器）。
- 缩放（scale）：寄存器只能存整数，小数怎么办？约定"实际值×100 存进去"，
  读出来再除以 100。比如压力 12.34 kN/m 存成 1234。
- 有符号数（int16）：16 位格子默认只能存 0~65535，但温度/Lab 值可能是负数。
  行业约定用"二进制补码"：>=32768 的数减去 65536 就是真实负值，
  from_i16() 函数做的就是这件事。

布局总览
========
unit 1 = 印刷机 PLC：
    0-9        全局区：车速/湿度/温度/机组数/机况/心跳
    10 起      每机组 16 个寄存器：
               +0..3   实际压力 ×100（kN/m，只读，传感器测到的，含模型调节效果）
               +4..7   初始压力 ×100（只读，人工调试的固定基准，印刷结束后用它复位）
               +8..11  设定压力 ×100（可写！控制端/模型把调节指令写到这里）
               +12     墨辊温度 ×10（有符号）
               +13     状态字（1=合格 2=超差）

unit 2 = 在线分光光度计：
    0-9        全局区：机组数/测量序号/状态
    10 起      每机组 14 个寄存器：
               +0..2   标准 L*a*b* ×100（有符号）——客户要的目标颜色
               +3..5   实测 L*a*b* ×100（有符号）——仪器测出来的颜色
               +6      ΔE(CIEDE2000) ×100 ——色差：两个颜色差多少（0=完全一样）
               +7      ΔD(密度差) ×100（有符号）——油墨密度的偏差
               +8      实测密度 ×100
               +9      状态字
               +10     测量序号

改动布局时只需要改这里，然后模拟器和采集端自动跟随。
"""

# ===== unit id（设备编号）=====
PLC_UNIT_ID = 1
SPECTRO_UNIT_ID = 2

# ===== 缩放系数：寄存器原始值 ÷ 缩放 = 工程实际值 =====
SPEED_SCALE = 10      # 车速 m/min，精确到 0.1
TEMP_SCALE = 10       # 温度 ℃ / 湿度 %RH
PRESSURE_SCALE = 100  # 压力 kN/m
COLOR_SCALE = 100     # Lab / ΔE / ΔD / 密度

# ===== PLC（unit 1）：全局区寄存器地址 =====
PLC_GLOBAL_COUNT = 10        # 0-9 号留给全局，10 号起才是机组数据
PLC_REG_SPEED = 0            # 车速
PLC_REG_HUMIDITY = 1         # 环境湿度
PLC_REG_TEMPERATURE = 2      # 车间温度（有符号，可能低于 0）
PLC_REG_GROUP_COUNT = 3      # 机组总数（采集端启动时先读它，才知道要读多少）
PLC_REG_MACHINE_STATUS = 4   # 整机状态字（1=运行中）
PLC_REG_HEARTBEAT = 5        # 心跳计数器，每秒+1。采集端看它变化就知道 PLC 活着

# ===== PLC 每机组的寄存器内偏移（机组起始地址 + 偏移 = 真实地址） =====
GROUP_REG_COUNT = 16          # 每机组占 16 个格子
PLC_P_ACTUAL_OFF = 0          # +0..3 实际压力（4 个模块各一个）
PLC_P_INIT_OFF = 4            # +4..7 初始压力（人工调试基准，固定不变，重置的依据）
PLC_P_SET_OFF = 8             # +8..11 设定压力（唯一可写的区域，闭环调节写这里）
PLC_ROLLER_TEMP_OFF = 12      # +12 墨辊温度
PLC_GROUP_STATUS_OFF = 13     # +13 机组状态字

PLC_BLOCK_SIZE = 512          # PLC 整块寄存器的大小（要 >= 10 + 12机组×16）

# ===== 分光光度计（unit 2）：全局区 =====
SPECTRO_GLOBAL_COUNT = 10
SPECTRO_REG_GROUP_COUNT = 0   # 机组数
SPECTRO_REG_MEASURE_SEQ = 1   # 全局测量序号（每测一轮 +1）
SPECTRO_REG_STATUS = 2        # 仪器状态

SPECTRO_REG_COUNT = 14        # 每机组占 14 个格子
SP_L_STD_OFF = 0              # +0..2 标准 Lab（3 个格子：L*, a*, b*）
SP_L_MEAS_OFF = 3             # +3..5 实测 Lab
SP_DELTA_E_OFF = 6            # +6 色差 ΔE
SP_DELTA_D_OFF = 7            # +7 密度差 ΔD（有符号，可正可负）
SP_DENSITY_OFF = 8            # +8 实测密度
SP_STATUS_OFF = 9             # +9 状态字
SP_SEQ_OFF = 10               # +10 该机组的测量序号

SPECTRO_BLOCK_SIZE = 256      # 分光光度计整块寄存器大小


def plc_group_base(group_index: int) -> int:
    """PLC 第 group_index 机组（0 起）的寄存器起始地址。

    例：2 号机组（group_index=1）=> 10 + 1×16 = 26，
    它的 1 号压力模块实际值就在 26 号格子。
    """
    return PLC_GLOBAL_COUNT + group_index * GROUP_REG_COUNT


def spectro_group_base(group_index: int) -> int:
    """分光光度计第 group_index 机组的寄存器起始地址。"""
    return SPECTRO_GLOBAL_COUNT + group_index * SPECTRO_REG_COUNT


def to_i16(value: int) -> int:
    """有符号 int16 → 16 位寄存器值（写入前用，二进制补码）。"""
    return value & 0xFFFF


def from_i16(value: int) -> int:
    """16 位寄存器值 → 有符号 int16（读出后用）。

    例：65533 读出来是 -3（因为 65533 - 65536 = -3）。
    """
    return value - 65536 if value >= 0x8000 else value


# ===== 点位模板（给 db.init_db 写入 points 表用） =====
# scope='global'：register_addr 是绝对地址（全机只有一份，比如车速）
# scope='group' ：register_addr 是机组内偏移（每个机组都有同名点位，比如 1 号压力）
def build_point_templates() -> list[dict]:
    """生成点位表种子数据。points 表存的就是这张"地址表"本身，
    前端/智能体查它就知道每个数据项的地址和单位。"""
    rows: list[dict] = []

    def add(device_code: str, code: str, name: str, unit: str, scope: str,
            addr: int, scale: int, signed: bool, writable: bool):
        rows.append({
            "device_code": device_code, "code": code, "name": name, "unit": unit,
            "scope": scope, "register_addr": addr, "scale": scale,
            "signed": int(signed), "writable": int(writable),
        })

    # --- PLC 全局点位 ---
    add("PLC-01", "speed", "车速", "m/min", "global", PLC_REG_SPEED, SPEED_SCALE, False, False)
    add("PLC-01", "humidity", "环境湿度", "%RH", "global", PLC_REG_HUMIDITY, TEMP_SCALE, False, False)
    add("PLC-01", "temperature", "车间温度", "℃", "global", PLC_REG_TEMPERATURE, TEMP_SCALE, True, False)
    add("PLC-01", "group_count", "机组数", "-", "global", PLC_REG_GROUP_COUNT, 1, False, False)
    add("PLC-01", "machine_status", "机况字", "-", "global", PLC_REG_MACHINE_STATUS, 1, False, False)
    add("PLC-01", "heartbeat", "PLC心跳", "-", "global", PLC_REG_HEARTBEAT, 1, False, False)
    # --- PLC 机组内点位（×4 压力模块）---
    for z in range(4):
        add("PLC-01", f"p_actual_{z + 1}", f"{z + 1}号压力模块实际值", "kN/m", "group",
            PLC_P_ACTUAL_OFF + z, PRESSURE_SCALE, True, False)
        add("PLC-01", f"p_init_{z + 1}", f"{z + 1}号压力模块初始值", "kN/m", "group",
            PLC_P_INIT_OFF + z, PRESSURE_SCALE, True, False)
        add("PLC-01", f"p_set_{z + 1}", f"{z + 1}号压力模块设定值", "kN/m", "group",
            PLC_P_SET_OFF + z, PRESSURE_SCALE, True, True)
    add("PLC-01", "roller_temp", "墨辊温度", "℃", "group", PLC_ROLLER_TEMP_OFF, TEMP_SCALE, True, False)
    add("PLC-01", "group_status", "机组状态字", "-", "group", PLC_GROUP_STATUS_OFF, 1, False, False)

    # --- 分光光度计点位 ---
    add("SPECTRO-01", "group_count", "机组数", "-", "global",
        SPECTRO_REG_GROUP_COUNT, 1, False, False)
    add("SPECTRO-01", "measure_seq", "测量序号", "-", "global",
        SPECTRO_REG_MEASURE_SEQ, 1, False, False)
    add("SPECTRO-01", "l_std", "标准L*", "-", "group", SP_L_STD_OFF, COLOR_SCALE, True, False)
    add("SPECTRO-01", "a_std", "标准a*", "-", "group", SP_L_STD_OFF + 1, COLOR_SCALE, True, False)
    add("SPECTRO-01", "b_std", "标准b*", "-", "group", SP_L_STD_OFF + 2, COLOR_SCALE, True, False)
    add("SPECTRO-01", "l_meas", "实测L*", "-", "group", SP_L_MEAS_OFF, COLOR_SCALE, True, False)
    add("SPECTRO-01", "a_meas", "实测a*", "-", "group", SP_L_MEAS_OFF + 1, COLOR_SCALE, True, False)
    add("SPECTRO-01", "b_meas", "实测b*", "-", "group", SP_L_MEAS_OFF + 2, COLOR_SCALE, True, False)
    add("SPECTRO-01", "delta_e", "色差ΔE(CIEDE2000)", "-", "group", SP_DELTA_E_OFF, COLOR_SCALE, False, False)
    add("SPECTRO-01", "delta_d", "密度差ΔD", "-", "group", SP_DELTA_D_OFF, COLOR_SCALE, True, False)
    add("SPECTRO-01", "density", "实测密度", "-", "group", SP_DENSITY_OFF, COLOR_SCALE, False, False)
    return rows
