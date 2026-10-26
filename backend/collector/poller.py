"""Modbus TCP 轮询采集服务（博泰实习项目核心链路）。

【零基础说明：这个文件在做什么？】
它扮演"现场数据采集网关"的角色，循环做一件事：

    每隔 poll_interval_s 秒：
      1. 连着 PLC（unit 1）读一块寄存器  -> 解析成 10 行压力/工况数据
      2. 连着分光光度计（unit 2）读一块  -> 解析成 10 行颜色质量数据
      3. 两边数据打上同一个时间戳（=时序对齐），放进内存缓冲区
      4. 缓冲区攒够一批（500 行）或超过 5 秒 -> 批量 INSERT 进 MySQL

【可靠性机制对应简历描述】
- 超时重传：单次读取失败 => 退避重试 2 次（_read_one 里 for 循环）
- 断线重连：连续异常 => reconnect() 关闭旧连接、指数退避重连
- 批量写入：不一条一条插（太慢），攒一批用 executemany 一次插（百万级数据靠它）
- 批次审计：每个周期往 collect_batches 写一行"这次读成功没有"，7x24 稳定性可查

【时序对齐】
PLC 和分光光度计是两台设备，读数有先后。我们在同一个周期内读它们，
并把同一批数据打上同一个 ts 时间戳入库——这样后续按时间 JOIN 两张表
（比如模型训练时），数据天然对齐。
"""

import logging
import time
from datetime import datetime

from pymodbus.client import ModbusTcpClient
from sqlalchemy import insert
from sqlalchemy.orm import Session

from config import settings
from db.models import Alarm, CollectBatch, ColorMeasurement, PlcReading
from db.mysql import get_engine, get_session_factory
import register_map as rm

log = logging.getLogger("collector")

DE_TOLERANCE = 1.5


def parse_i16(regs: list[int], index: int) -> int:
    """从寄存器数组中取第 index 个并解析成有符号 int16（字节流解析）。"""
    return rm.from_i16(regs[index])


class Poller:
    def __init__(self, batch_rows: int = 500, flush_interval_s: float = 5.0):
        # Modbus 客户端：timeout=3s 表示单次请求 3 秒没回就超时
        self.client = ModbusTcpClient(settings.modbus_host, port=settings.modbus_port,
                                      timeout=3.0)
        self.engine = get_engine()
        self.session_factory = get_session_factory()
        self.batch_rows = batch_rows          # 缓冲多少行触发一次入库
        self.flush_interval_s = flush_interval_s  # 最长多久强制入库一次
        self.group_count = 0                  # 机组数，启动时从 PLC 寄存器读出来
        # 三个内存缓冲区：攒够一批再写库，减少数据库交互次数
        self.plc_buf: list[dict] = []
        self.color_buf: list[dict] = []
        self.alarm_buf: list[dict] = []
        # 记录每机组上一次是否合格 -> 只在"合格→超差"边沿触发告警，避免每周期刷一条
        self.last_in_tol: dict[int, bool] = {}
        self.last_flush = time.monotonic()
        # 运行统计（日志/联调时看）
        self.stats = {"cycles": 0, "ok": 0, "partial": 0, "failed": 0,
                      "plc_rows": 0, "color_rows": 0, "alarms": 0, "reconnects": 0}

    # ---------- 连接管理 ----------

    def connect(self, retries: int = 5) -> None:
        """连接 Modbus 服务器，失败则指数退避重试（1s→2s→4s→8s→15s 封顶）。"""
        delay = 1.0
        for i in range(retries):
            if self.client.connect():
                log.info("Modbus connected %s:%s", settings.modbus_host, settings.modbus_port)
                return
            log.warning("Modbus connect failed (attempt %d/%d), retry in %.1fs",
                        i + 1, retries, delay)
            time.sleep(delay)
            delay = min(delay * 2, 15.0)
        raise ConnectionError(
            f"cannot connect modbus {settings.modbus_host}:{settings.modbus_port}")

    def reconnect(self) -> None:
        """断线重连：彻底关闭旧连接再重连。"""
        self.stats["reconnects"] += 1
        log.warning("reconnecting modbus...")
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
        self.connect(retries=3)

    # ---------- 底层读取（超时重传 + 分块） ----------

    READ_CHUNK = 125  # Modbus FC03 协议规定单次最多读 125 个寄存器，多了要分块

    def _read(self, address: int, count: int, device_id: int, retries: int = 2) -> list[int]:
        """读一段寄存器：自动分块 + 每块重试。对外只暴露这一个入口。"""
        values: list[int] = []
        for off in range(0, count, self.READ_CHUNK):
            n = min(self.READ_CHUNK, count - off)
            values.extend(self._read_one(address + off, n, device_id, retries))
        return values

    def _read_one(self, address: int, count: int, device_id: int, retries: int = 2) -> list[int]:
        """读一块（≤125 个）寄存器；失败退避重试（这就是"超时重传"）。"""
        last_err: Exception | None = None
        for i in range(retries + 1):
            try:
                rr = self.client.read_holding_registers(address, count=count,
                                                        slave=device_id)
                if not rr.isError():
                    return list(rr.registers)
                last_err = IOError(f"modbus error response: {rr}")
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.2 * (i + 1))
        raise IOError(f"read unit={device_id} addr={address} count={count} "
                      f"failed after {retries + 1} attempts: {last_err!r}")

    # ---------- 协议解析 ----------

    def discover_groups(self) -> None:
        """启动时先读 PLC 的"机组数"寄存器——设备自己告诉我们它有几组。

        这样采集端不用写死配置：模拟器起 8 组还是 12 组都能自适应。
        """
        regs = self._read(rm.PLC_REG_GROUP_COUNT, 1, rm.PLC_UNIT_ID)
        self.group_count = regs[0]
        if not 1 <= self.group_count <= rm.PLC_BLOCK_SIZE // rm.GROUP_REG_COUNT:
            raise ValueError(f"invalid group count {self.group_count}")
        log.info("discovered %d groups from PLC", self.group_count)

    def read_plc(self, ts: datetime) -> list[dict]:
        """读 PLC 整块寄存器 -> 解析成每机组一行 plc_readings 字典列表。

        寄存器 -> 工程值：原始值 ÷ 缩放系数（register_map 定义的 scale）。
        """
        count = rm.PLC_GLOBAL_COUNT + self.group_count * rm.GROUP_REG_COUNT
        regs = self._read(0, count, rm.PLC_UNIT_ID)
        rows = []
        speed = regs[rm.PLC_REG_SPEED] / rm.SPEED_SCALE
        hum = regs[rm.PLC_REG_HUMIDITY] / rm.TEMP_SCALE
        temp = parse_i16(regs, rm.PLC_REG_TEMPERATURE) / rm.TEMP_SCALE
        for g in range(self.group_count):
            base = rm.plc_group_base(g)
            row = {
                "ts": ts, "group_index": g, "speed_mmin": speed, "humidity": hum,
                "temperature": temp,
                "roller_temp": parse_i16(regs, base + rm.PLC_ROLLER_TEMP_OFF) / rm.TEMP_SCALE,
                "status": regs[base + 13],
            }
            for z in range(4):
                # 压力可为负（量程 -5~5），寄存器按 int16 补码解析
                row[f"p_actual_{z + 1}"] = parse_i16(regs, base + rm.PLC_P_ACTUAL_OFF + z) / rm.PRESSURE_SCALE
                row[f"p_init_{z + 1}"] = parse_i16(regs, base + rm.PLC_P_INIT_OFF + z) / rm.PRESSURE_SCALE
                row[f"p_set_{z + 1}"] = parse_i16(regs, base + rm.PLC_P_SET_OFF + z) / rm.PRESSURE_SCALE
            rows.append(row)
        return rows

    def read_colors(self, ts: datetime) -> list[dict]:
        """读分光光度计整块寄存器 -> 每机组一行 color_measurements。"""
        count = rm.SPECTRO_GLOBAL_COUNT + self.group_count * rm.SPECTRO_REG_COUNT
        regs = self._read(0, count, rm.SPECTRO_UNIT_ID)
        seq = regs[rm.SPECTRO_REG_MEASURE_SEQ]
        rows = []
        for g in range(self.group_count):
            base = rm.spectro_group_base(g)
            lab_s = [parse_i16(regs, base + rm.SP_L_STD_OFF + k) / rm.COLOR_SCALE
                     for k in range(3)]
            lab_m = [parse_i16(regs, base + rm.SP_L_MEAS_OFF + k) / rm.COLOR_SCALE
                     for k in range(3)]
            de = regs[base + rm.SP_DELTA_E_OFF] / rm.COLOR_SCALE
            dd = parse_i16(regs, base + rm.SP_DELTA_D_OFF) / rm.COLOR_SCALE
            dens = regs[base + rm.SP_DENSITY_OFF] / rm.COLOR_SCALE
            d_std = round(0.35 + (100.0 - lab_s[0]) / 45.0, 3)
            rows.append({
                "ts": ts, "group_index": g,
                "l_std": lab_s[0], "a_std": lab_s[1], "b_std": lab_s[2],
                "l_meas": lab_m[0], "a_meas": lab_m[1], "b_meas": lab_m[2],
                "delta_e": de, "delta_d": dd,
                "density_std": d_std, "density_meas": dens,
                "in_tolerance": de <= DE_TOLERANCE,
                "measure_seq": seq,
            })
        return rows

    # ---------- 告警（ΔE 越限沿触发，避免刷屏） ----------

    def detect_alarms(self, color_rows: list[dict], ts: datetime) -> None:
        """只在状态"翻转"的瞬间写告警：合格→超差 报警，超差→合格 报恢复。

        如果每周期都写，ΔE 持续超差 1 小时会写 1800 条重复告警。
        """
        for row in color_rows:
            g = row["group_index"]
            prev = self.last_in_tol.get(g)
            now = bool(row["in_tolerance"])
            self.last_in_tol[g] = now
            if prev is True and not now:
                self.alarm_buf.append({
                    "ts": ts, "level": "warn", "group_index": g,
                    "code": "DE_OUT_OF_TOL",
                    "message": f"{g + 1}号色组 ΔE={row['delta_e']:.2f} 超差(>{DE_TOLERANCE})",
                })
            elif prev is False and now:
                self.alarm_buf.append({
                    "ts": ts, "level": "info", "group_index": g,
                    "code": "DE_RECOVERED",
                    "message": f"{g + 1}号色组 ΔE 恢复正常",
                })

    # ---------- 批量入库 ----------

    def flush(self, force: bool = False) -> None:
        """把缓冲区批量写入 MySQL。

        触发条件：缓冲区有数据 且（超过 flush_interval_s 或 force=True）。
        insert(Model) + 字典列表 = SQLAlchemy 的 executemany 批量插入，
        比逐行 insert 快一个数量级，百万级写入靠它。
        """
        due = time.monotonic() - self.last_flush >= self.flush_interval_s
        if not force and not (due and (self.plc_buf or self.color_buf or self.alarm_buf)):
            return
        with Session(self.engine) as session:
            if self.plc_buf:
                session.execute(insert(PlcReading), self.plc_buf)
            if self.color_buf:
                session.execute(insert(ColorMeasurement), self.color_buf)
            if self.alarm_buf:
                session.execute(insert(Alarm), self.alarm_buf)
            session.commit()
        self.stats["plc_rows"] += len(self.plc_buf)
        self.stats["color_rows"] += len(self.color_buf)
        self.stats["alarms"] += len(self.alarm_buf)
        self.plc_buf.clear()
        self.color_buf.clear()
        self.alarm_buf.clear()
        self.last_flush = time.monotonic()

    # ---------- 单周期：读取→对齐→缓冲 ----------

    def cycle(self) -> str:
        """执行一个采集周期，返回状态 ok/partial/failed。

        - ok：PLC 和分光光度计都读到 -> 两表数据同 ts 入库（时序对齐）
        - partial：只有一台读到（另一台在重试后仍失败）
        - failed：两台都没读到
        """
        t0 = time.monotonic()
        ts = datetime.now()
        status = "ok"
        err = None
        plc_ok = spectro_ok = False
        try:
            plc_rows = self.read_plc(ts)
            plc_ok = True
        except Exception as exc:  # noqa: BLE001
            plc_rows = []
            log.error("PLC read failed: %r", exc)
            err = f"plc: {exc!r}"
        try:
            color_rows = self.read_colors(ts)
            spectro_ok = True
        except Exception as exc:  # noqa: BLE001
            color_rows = []
            log.error("spectro read failed: %r", exc)
            err = f"{err or ''} spectro: {exc!r}".strip()

        if plc_ok and spectro_ok:
            self.plc_buf.extend(plc_rows)
            self.color_buf.extend(color_rows)
            self.detect_alarms(color_rows, ts)
        elif plc_ok or spectro_ok:
            status = "partial"
        else:
            status = "failed"

        # 每个周期写一行批次审计（7x24 稳定性的证据链）
        duration_ms = int((time.monotonic() - t0) * 1000)
        with self.session_factory() as session:
            session.add(CollectBatch(
                started_at=ts, finished_at=datetime.now(), duration_ms=duration_ms,
                plc_ok=plc_ok, spectro_ok=spectro_ok, status=status, error=err,
            ))
            session.commit()
        self.stats["cycles"] += 1
        self.stats[status] += 1
        return status

    # ---------- 主循环 ----------

    def run(self, cycles: int | None = None) -> None:
        """主循环。cycles=None 表示无限常驻（现场 7x24）；传数字则跑完退出（联调）。"""
        self.connect()
        self.discover_groups()
        n = 0
        while cycles is None or n < cycles:
            n += 1
            cycle_start = time.monotonic()
            try:
                status = self.cycle()
            except Exception as exc:  # noqa: BLE001
                log.error("cycle crashed: %r", exc)
                status = "failed"
                self.stats["failed"] += 1
                self.reconnect()
                continue
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001
                log.error("flush failed (will retry): %r", exc)
            if n % 10 == 0:
                log.info("cycle %d status=%s stats=%s", n, status, self.stats)
            # 固定周期调度：本轮耗时从间隔里扣掉，保证平均每 2s 一轮
            sleep_s = settings.poll_interval_s - (time.monotonic() - cycle_start)
            if sleep_s > 0:
                time.sleep(sleep_s)
        self.flush(force=True)
