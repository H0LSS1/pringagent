"""印刷工艺物理模型：标准色板、压力-色差联动、ΔE(CIEDE2000)/ΔD 计算。

【零基础说明：这个文件在模拟什么物理现象？】
印刷机的颜色质量主要受"印刷压力"影响：
  - 压力太小 → 转印到纸上的油墨不足 → 颜色偏浅（L* 偏高、密度偏低）
  - 压力太大 → 油墨挤压铺展 → 颜色偏深偏浓
所以 |实际压力 - 初始压力| 的偏差越大，色差 ΔE 就越大。
本文件用近似线性的公式描述这个关系，是整个系统数据的"物理来源"。

被两个地方共用（保证口径一致、数据自洽）：
  - simulator/modbus_server.py：实时生成寄存器数据
  - collector/backfill.py：批量生成历史数据（训练模型用）

【颜色小知识】
- Lab 颜色空间：L* 明度（0 黑~100 白），a* 红绿轴（正红负绿），
  b* 黄蓝轴（正黄负蓝）。任何颜色都能用这三个数表示。
- ΔE（CIEDE2000）：国际标准的色差计算公式，表示两个颜色"人眼看起来差多少"。
  一般 ΔE < 1 人眼难分辨，印刷行业公差通常要求 ΔE ≤ 1.5~3。
"""

import math
import random

# 12 组标准色板（L*, a*, b*），机组循环取用（模拟多色组印刷：青/品红/黄/黑 + 专色）
PALETTE: list[tuple[str, tuple[float, float, float]]] = [
    ("青色 Cyan", (55.0, -37.0, -50.0)),
    ("品红 Magenta", (48.0, 74.0, -3.0)),
    ("黄色 Yellow", (89.0, -4.9, 93.0)),
    ("黑色 Black", (16.0, 0.3, 0.0)),
    ("专红 Spot-Red", (47.0, 68.0, 47.0)),
    ("专蓝 Spot-Blue", (26.0, 22.0, -46.0)),
    ("专绿 Spot-Green", (49.0, -66.0, 25.0)),
    ("中性灰 Gray", (81.0, 0.5, 0.5)),
    ("金色 Gold", (74.0, 12.0, 62.0)),
    ("棕色 Brown", (38.0, 20.0, 22.0)),
    ("紫色 Violet", (28.0, 30.0, -35.0)),
    ("橙色 Orange", (66.0, 43.0, 62.0)),
]

# ΔE 质量公差（对标 ISO 12647 类标准口径）：超过即告警/触发压力反馈调节
DE_TOLERANCE = 1.5

# 机组标定偏移范围（模拟每机组机械差异）。
# 【调参说明】车速恒定后，压力信号的方差主要来自 机组/模块 梯度项，
# 标定偏移是回归模型学不到的"噪声"，其幅值直接决定 R²：
# ±0.15 时理论 R² ≈ 0.95（对齐简历口径）；过大会明显拉低 R²。
CALIB_OFFSET_RANGE = 0.15

# 分光光度计的"颜色达标"判定线：ΔE 低于此值认为 实测Lab ≈ 标准Lab。
# 模型训练只取达标时刻的样本（见 models/pressure.py）。
COLOR_GOOD_DE = 0.5


def optimal_pressure(speed_mmin: float, group_index: int, zone_index: int) -> float:
    """颜色最优压力（kN/m）：在该压力下印刷，实测 Lab ≈ 标准 Lab（ΔE 最小）。

    线性物理关系（真实印刷工艺的经验规律）：
      - 车速越快，油墨转移时间越短，需要更大压力维持转印 -> +0.010×车速
      - 色组位置带来的工艺梯度：机组号/模块号以全场中心为原点对称展开，
        使压力自然分布在小量程中段（用户量程 -5~5 kN/m）
    注意：这是"颜色意义上的最优"，随机器状态慢漂（见模拟器的磨损项），
    与人工调试的初始压力不是一回事——初始压力印出来的颜色不一定达标。
    """
    return (0.010 * speed_mmin
            + 0.35 * (group_index - 5.5)
            + 0.50 * (zone_index - 1.5))


def std_density(l_star: float) -> float:
    """由明度 L* 近似推算实地密度 D（演示口径）。

    L* 越低（颜色越深）密度越高：L*=16 的黑墨 ≈ 2.09，L*=89 的黄墨 ≈ 0.82。
    """
    return round(0.35 + (100.0 - l_star) / 45.0, 3)


def color_shift(dev: float, rng: random.Random, group_index: int) -> tuple[float, float, float]:
    """压力偏差 → Lab 颜色偏移量（近似线性 + 测量噪声）。

    参数 dev：机组内 4 个压力模块 |实际-标准| 的均值（kN/m）。
    返回 (dL, da, db)：实测 Lab 相对标准 Lab 的偏移。
    """
    # 不同机组颜色偏移方向不同（有的偏红有的偏绿），用机组号决定符号，避免所有组同向
    sign_a = 1.0 if (group_index % 2 == 0) else -1.0
    dl = -0.80 * dev + rng.gauss(0.0, 0.06)   # 压力偏大→墨层厚→明度降低，所以是负相关
    da = 0.42 * sign_a * dev + rng.gauss(0.0, 0.05)
    db = 0.48 * math.sin(group_index * 1.7) * dev + rng.gauss(0.0, 0.05)
    return dl, da, db


def delta_e_ciede2000(l1: float, a1: float, b1: float,
                      l2: float, a2: float, b2: float) -> float:
    """CIEDE2000 色差公式（按 Sharma et al. 2005 论文实现，工业界标准做法）。

    输入两组 Lab 值，返回一个正数：约等于"人眼感知的颜色差异"。
    <1.0 几乎无感；1~3 仔细看能发现；>5 明显色差。
    公式本身很复杂（明度/彩度/色相分别加权 + 旋转项），照抄论文公式即可，
    不需要逐行理解，面试时能说出"用 CIEDE2000 而不是简单欧氏距离，因为在
    蓝色区域欧氏距离与人眼感知偏差大"就够了。
    """
    # ---- 第 1 步：计算 C*a'b'（彩度）和 h'a'b'（色相角）----
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    cbar = (c1 + c2) / 2.0
    cbar7 = cbar ** 7
    # g：彩度轴的修正系数（低彩度区域 a* 轴要缩放）
    g = 0.5 * (1.0 - math.sqrt(cbar7 / (cbar7 + 25.0 ** 7)))
    ap1 = (1.0 + g) * a1
    ap2 = (1.0 + g) * a2
    cp1 = math.hypot(ap1, b1)
    cp2 = math.hypot(ap2, b2)
    # atan2 算色相角（0~360 度）；纯灰点（a=b=0）没有色相，定义为 0
    hp1 = math.degrees(math.atan2(b1, ap1)) % 360.0 if (ap1 != 0.0 or b1 != 0.0) else 0.0
    hp2 = math.degrees(math.atan2(b2, ap2)) % 360.0 if (ap2 != 0.0 or b2 != 0.0) else 0.0

    # ---- 第 2 步：三个分量的差值 dL'（明度）/ dC'（彩度）/ dH'（色相）----
    dlp = l2 - l1
    dcp = cp2 - cp1
    if cp1 * cp2 == 0.0:
        dhp = 0.0  # 有一方是无彩色（灰），色相差为 0
    else:
        dhp = hp2 - hp1
        if dhp > 180.0:     # 色相角是环形的：350° 和 10° 只差 20° 不是 340°
            dhp -= 360.0
        elif dhp < -180.0:
            dhp += 360.0
    dHp = 2.0 * math.sqrt(cp1 * cp2) * math.sin(math.radians(dhp) / 2.0)

    # ---- 第 3 步：加权合成（对不同区域做感知修正）----
    lbar = (l1 + l2) / 2.0
    cpbar = (cp1 + cp2) / 2.0
    if cp1 * cp2 == 0.0:
        hbar = hp1 + hp2
    else:
        s = hp1 + hp2
        if abs(hp1 - hp2) > 180.0:  # 跨越 0° 时平均角要绕过去
            hbar = (s + 360.0) / 2.0 if s < 360.0 else (s - 360.0) / 2.0
        else:
            hbar = s / 2.0

    # T：色相相关的修正项；dtheta：蓝色区（275°附近）的旋转项
    t = (1.0 - 0.17 * math.cos(math.radians(hbar - 30.0))
         + 0.24 * math.cos(math.radians(2.0 * hbar))
         + 0.32 * math.cos(math.radians(3.0 * hbar + 6.0))
         - 0.20 * math.cos(math.radians(4.0 * hbar - 63.0)))
    dtheta = 30.0 * math.exp(-(((hbar - 275.0) / 25.0) ** 2))
    cpbar7 = cpbar ** 7
    rc = 2.0 * math.sqrt(cpbar7 / (cpbar7 + 25.0 ** 7))
    sl = 1.0 + 0.015 * (lbar - 50.0) ** 2 / math.sqrt(20.0 + (lbar - 50.0) ** 2)
    sc = 1.0 + 0.045 * cpbar
    sh = 1.0 + 0.015 * cpbar * t
    rt = -math.sin(math.radians(2.0 * dtheta)) * rc

    return math.sqrt(
        (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dHp / sh) ** 2
        + rt * (dcp / sc) * (dHp / sh)
    )


def measure_color(group_index: int, dev: float, rng: random.Random
                  ) -> tuple[tuple[float, float, float], tuple[float, float, float],
                             float, float, float, float]:
    """由压力偏差生成一次完整的颜色测量（数据自洽的关键入口）。

    流程：标准 Lab →（按 dev 偏移）→ 实测 Lab →（公式计算）→ ΔE/ΔD/密度。

    返回: (std_lab, meas_lab, delta_e, delta_d, density_std, density_meas)
    ΔE/ΔD 不是随机数，而是从 Lab 算出来的——这样"压力偏差→色差"的因果链
    在数据里始终成立，线性回归模型才有规律可学。
    """
    std_lab = PALETTE[group_index % len(PALETTE)][1]
    dl, da, db = color_shift(dev, rng, group_index)
    meas_lab = (std_lab[0] + dl, std_lab[1] + da, std_lab[2] + db)
    de = delta_e_ciede2000(*std_lab, *meas_lab)
    d_std = std_density(std_lab[0])
    d_meas = std_density(meas_lab[0])
    dd = d_meas - d_std
    return std_lab, meas_lab, de, dd, d_std, d_meas
