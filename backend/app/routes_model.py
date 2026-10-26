"""模型与闭环控制接口：训练/查询激活模型 + 压力自动反馈调节（双向通信下行）。

【零基础说明：什么是"闭环调节"？】
闭环 = 把输出反馈回输入来纠正偏差。本系统的闭环：

    1.【上行/读】从 PLC 读当前车速/温湿度，从分光光度计读 ΔE/ΔD（实时值！）
    2.【判断】哪些机组 ΔE 超过公差（默认 1.5）？——这些是需要调节的
    3.【预测】压力模型根据 分光光度计数据（ΔE/ΔD，即实测Lab与标准Lab的差距）
       预测"能让实测 Lab 逼近标准 Lab"的压力（从颜色达标时刻的历史样本学得）
    4.【下行/写】把预测值写进 PLC 的"设定压力"寄存器（这就是 Modbus 双向通信）
    5.【收敛】模拟器里实际压力会一阶惯性向新设定值靠拢 -> ΔE 降下来
    6.【审计】每一步都写 pressure_predictions（预测记录）和 control_logs（操作日志）

为什么用"实时读寄存器"而不是查数据库最新行？
数据库里的行是采集周期（2 秒前）写进去的，车速一直在变，闭环控制必须用
当下最新的工况——这是工业控制的基本要求（消除滞后）。

注意 dry_run 参数：dry_run=True 时只预测和记录、不真正写 PLC（联调安全）。
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pymodbus.client import ModbusTcpClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from db.models import ControlLog, ModelRegistry, PressurePrediction
from db.mysql import get_session_factory
import register_map as rm
from models import pressure as pressure_model

router = APIRouter(tags=["model"])

READ_CHUNK = 125  # Modbus FC03 单次最多 125 个寄存器，超过要分块读


class TrainRequest(BaseModel):
    """POST /api/model/train 的请求体（FastAPI 自动做字段校验）。"""
    limit: int = Field(100000, ge=1000, le=1000000, description="训练样本上限")


class AdjustRequest(BaseModel):
    """POST /api/control/adjust 的请求体。

    group_indices 不传 => 自动选 ΔE 超差机组；传了 => 强制调节指定机组。
    """
    group_indices: list[int] | None = Field(None, description="目标机组；空=自动选 ΔE 超差机组")
    tolerance: float = Field(1.5, gt=0, description="ΔE 超差阈值")
    dry_run: bool = Field(False, description="只预测与记录，不下发 PLC")
    operator: str = Field("api", max_length=64)


class ResetRequest(BaseModel):
    """POST /api/control/reset 的请求体（印刷结束后的人工复位）。"""
    group_indices: list[int] | None = Field(None, description="目标机组；空=全部机组")
    operator: str = Field("api", max_length=64)


def _read_block(client: ModbusTcpClient, unit: int, address: int, count: int) -> list[int]:
    """分块读寄存器（每块 ≤125），拼成完整列表。失败抛异常。"""
    values: list[int] = []
    for off in range(0, count, READ_CHUNK):
        n = min(READ_CHUNK, count - off)
        rr = client.read_holding_registers(address + off, count=n, slave=unit)
        if rr.isError():
            raise IOError(f"read unit={unit} addr={address + off} failed: {rr}")
        values.extend(rr.registers)
    return values


def _active_model_row(session: Session) -> ModelRegistry:
    """取当前激活的模型版本；没有则返回 400（前端提示"请先训练"）。"""
    row = session.execute(
        select(ModelRegistry)
        .where(ModelRegistry.name == pressure_model.MODEL_NAME,
               ModelRegistry.is_active.is_(True))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=400, detail="无激活模型，请先 POST /api/model/train")
    return row


@router.post("/model/train")
def train_model(req: TrainRequest):
    """训练压力模型（同步执行；10 万样本约几秒）。训练完新版本自动激活。"""
    try:
        return pressure_model.train(limit=req.limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/model/active")
def active_model():
    """当前激活的模型版本、算法、指标（前端模型管理页用）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        row = _active_model_row(session)
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


@router.post("/control/adjust")
def control_adjust(req: AdjustRequest):
    """压力闭环调节（详见文件头说明）。返回每个机组的旧/新设定值与是否下发成功。"""
    # ---- 0. 连接 PLC（读和写共用这一个连接，减少握手次数） ----
    client = ModbusTcpClient(settings.modbus_host, port=settings.modbus_port, timeout=3.0)
    if not client.connect():
        raise HTTPException(status_code=502,
                            detail=f"PLC 不可达 {settings.modbus_host}:{settings.modbus_port}")

    ts = datetime.now()
    try:
        # ---- 1. 实时读取（上行） ----
        # 读 PLC：最多按 12 机组预留长度，多读无害（寄存器块够大）
        plc_regs = _read_block(client, rm.PLC_UNIT_ID, 0,
                               rm.PLC_GLOBAL_COUNT + 12 * rm.GROUP_REG_COUNT)
        group_count = plc_regs[rm.PLC_REG_GROUP_COUNT]
        speed = plc_regs[rm.PLC_REG_SPEED] / rm.SPEED_SCALE
        temperature = rm.from_i16(plc_regs[rm.PLC_REG_TEMPERATURE]) / rm.TEMP_SCALE
        humidity = plc_regs[rm.PLC_REG_HUMIDITY] / rm.TEMP_SCALE

        # 读分光光度计：每机组的 ΔE / ΔD
        sp_regs = _read_block(client, rm.SPECTRO_UNIT_ID, 0,
                              rm.SPECTRO_GLOBAL_COUNT + group_count * rm.SPECTRO_REG_COUNT)
        live: dict[int, dict] = {}
        for g in range(group_count):
            sp_base = rm.spectro_group_base(g)
            plc_base = rm.plc_group_base(g)
            live[g] = {
                "delta_e": sp_regs[sp_base + rm.SP_DELTA_E_OFF] / rm.COLOR_SCALE,
                "delta_d": rm.from_i16(sp_regs[sp_base + rm.SP_DELTA_D_OFF]) / rm.COLOR_SCALE,
                # 压力可为负：int16 补码解析
                "p_actual": [rm.from_i16(plc_regs[plc_base + rm.PLC_P_ACTUAL_OFF + z])
                             / rm.PRESSURE_SCALE for z in range(4)],
                "p_set": [rm.from_i16(plc_regs[plc_base + rm.PLC_P_SET_OFF + z])
                          / rm.PRESSURE_SCALE for z in range(4)],
            }

        # ---- 2. 选候选机组：显式指定 或 自动选超差机组 ----
        if req.group_indices:
            candidates = sorted(set(req.group_indices) & set(live))
        else:
            candidates = sorted(g for g, s in live.items() if s["delta_e"] > req.tolerance)
        if not candidates:
            return {"adjusted": [], "message": "无超差机组，无需调节",
                    "tolerance": req.tolerance,
                    "speed_mmin": speed}

        model_row = _active_model_row(Session(get_session_factory().kw["bind"]))

        # ---- 3. 模型预测"颜色达标压力"（实测Lab→标准Lab） ----
        samples = [{
            "group_index": g, "speed_mmin": speed, "temperature": temperature,
            "humidity": humidity, "delta_e": live[g]["delta_e"], "delta_d": live[g]["delta_d"],
        } for g in candidates]
        predictions = pressure_model.predict_std_pressures(samples)
        by_group: dict[int, dict[int, float]] = {}
        for p in predictions:
            by_group.setdefault(p["group_index"], {})[p["zone_index"]] = p["predicted"]

        # ---- 4. 写设定寄存器（下行）+ 审计入库 ----
        adjusted = []
        with Session(get_session_factory().kw["bind"]) as session:
            for g, zone_preds in by_group.items():
                base = rm.plc_group_base(g)
                # 预测值截断到安全范围 [-4.5, 4.5]，负数转 int16 补码后写寄存器
                regs = [rm.to_i16(int(min(4.5, max(-4.5, zone_preds[z])) * rm.PRESSURE_SCALE))
                        for z in range(4)]
                if req.dry_run:
                    write_ok = True  # 演练模式：不真写，只记录
                else:
                    rr = client.write_registers(base + rm.PLC_P_SET_OFF, regs,
                                                slave=rm.PLC_UNIT_ID)
                    write_ok = not rr.isError()

                for z in range(4):
                    new_v = regs[z] / rm.PRESSURE_SCALE
                    result = "dry_run" if req.dry_run else ("ok" if write_ok else "failed")
                    # 预测记录：调节可追溯（当时 ΔE 多少、从多少调到多少）
                    session.add(PressurePrediction(
                        ts=ts, model_id=model_row.id, group_index=g, zone_index=z,
                        delta_e=live[g]["delta_e"],
                        current_actual=live[g]["p_actual"][z], current_set=live[g]["p_set"][z],
                        predicted_std=new_v, applied=bool(not req.dry_run and write_ok),
                    ))
                    # 控制日志：谁(source)在何时把哪个寄存器从多少改成多少、为什么
                    session.add(ControlLog(
                        ts=ts, source="model", operator=req.operator,
                        group_index=g, zone_index=z, action="set_pressure",
                        register_addr=base + rm.PLC_P_SET_OFF + z,
                        old_value=live[g]["p_set"][z], new_value=new_v,
                        reason=f"ΔE={live[g]['delta_e']:.2f} 实时反馈调节@{speed:.0f}m/min",
                        result=result,
                    ))
                adjusted.append({
                    "group_index": g,
                    "delta_e": live[g]["delta_e"],
                    "old_setpoints": live[g]["p_set"],
                    # 写入的是 int16 补码，显示时转回有符号值（负压力不显示成 654.x）
                    "new_setpoints": [rm.from_i16(r) / rm.PRESSURE_SCALE for r in regs],
                    "written": bool(not req.dry_run and write_ok),
                })
            session.commit()
    finally:
        # 无论成功失败都断开连接（finally 保证异常时也执行）
        client.close()

    return {"adjusted": adjusted, "tolerance": req.tolerance, "dry_run": req.dry_run,
            "speed_mmin": speed, "model_version": model_row.version}

@router.post("/control/reset")
def control_reset(req: ResetRequest):
    """压力复位：把设定压力写回初始压力（人工调试基准），印刷结束后恢复初始状态。

    与 /control/adjust 的区别：adjust 是"模型算出该调多少"（反馈调节）；
    reset 是"不管当前多少，回到出厂基准"（人工复位），初始值固定不变。
    """
    client = ModbusTcpClient(settings.modbus_host, port=settings.modbus_port, timeout=3.0)
    if not client.connect():
        raise HTTPException(status_code=502,
                            detail=f"PLC 不可达 {settings.modbus_host}:{settings.modbus_port}")
    ts = datetime.now()
    try:
        plc_regs = _read_block(client, rm.PLC_UNIT_ID, 0,
                               rm.PLC_GLOBAL_COUNT + 12 * rm.GROUP_REG_COUNT)
        group_count = plc_regs[rm.PLC_REG_GROUP_COUNT]
        candidates = sorted(set(req.group_indices) & set(range(group_count))) \
            if req.group_indices else list(range(group_count))

        results = []
        with Session(get_session_factory().kw["bind"]) as session:
            for g in candidates:
                base = rm.plc_group_base(g)
                old_set = [rm.from_i16(plc_regs[base + rm.PLC_P_SET_OFF + z]) / rm.PRESSURE_SCALE
                           for z in range(4)]
                init = [rm.from_i16(plc_regs[base + rm.PLC_P_INIT_OFF + z]) / rm.PRESSURE_SCALE
                        for z in range(4)]
                regs = [rm.to_i16(int(v * rm.PRESSURE_SCALE)) for v in init]
                rr = client.write_registers(base + rm.PLC_P_SET_OFF, regs, slave=rm.PLC_UNIT_ID)
                write_ok = not rr.isError()
                for z in range(4):
                    session.add(ControlLog(
                        ts=ts, source="manual", operator=req.operator,
                        group_index=g, zone_index=z, action="reset_pressure",
                        register_addr=base + rm.PLC_P_SET_OFF + z,
                        old_value=old_set[z], new_value=init[z],
                        reason="复位到人工调试初始压力",
                        result="ok" if write_ok else "failed",
                    ))
                results.append({"group_index": g, "old_setpoints": old_set,
                                "initial": init, "written": bool(write_ok)})
            session.commit()
        return {"reset": results}
    finally:
        client.close()
