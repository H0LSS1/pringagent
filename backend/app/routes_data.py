"""数据服务 REST 接口（博泰实习项目复现：监控前端的数据来源）。

【零基础说明】
每个函数对应一个 HTTP 接口：
- @router.get("/xxx") 表示 GET /api/xxx（main.py 里 include 时加了 /api 前缀）
- 函数参数自动变成查询参数（如 ?hours=24），FastAPI 会做类型校验
- 函数返回 list/dict -> FastAPI 自动序列化成 JSON
- `with Session(...) as session`：一次请求开一个数据库会话，用完自动关闭
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.queries import bucket_agg, latest_per_group
from db.models import Alarm, CollectBatch, ColorMeasurement, Device, DeviceGroup, PlcReading
from db.mysql import get_session_factory

router = APIRouter(tags=["data"])


def _row_dict(obj) -> dict:
    """ORM 对象 -> 普通字典（FastAPI 才能序列化）。取该行所有列。"""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


@router.get("/devices")
def list_devices():
    """设备清单 + 各设备机组数（前端设备下拉框/状态卡片用）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        devices = session.execute(select(Device).order_by(Device.id)).scalars().all()
        n_groups = dict(session.execute(
            select(DeviceGroup.device_id, func.count())
            .group_by(DeviceGroup.device_id)
        ).all())
        return [{
            **_row_dict(d),
            "group_count": n_groups.get(d.id, 0),
        } for d in devices]


@router.get("/dashboard/latest")
def dashboard_latest():
    """监控大屏：每机组最新工况 + 最新色彩质量。

    PLC 行与颜色行取自各自表的最新记录。因为采集服务把同一周期的两类数据
    打上同一个 ts（时序对齐），正常情况下两者的 ts 完全一致，可直接拼接。
    """
    with Session(get_session_factory().kw["bind"]) as session:
        plc = latest_per_group(session, PlcReading)
        color = latest_per_group(session, ColorMeasurement)
        groups = sorted(set(plc) | set(color))
        out = []
        for g in groups:
            pr, cm = plc.get(g), color.get(g)
            out.append({
                "group_index": g,
                "ts": (pr.ts if pr else cm.ts) if (pr or cm) else None,
                "speed_mmin": pr.speed_mmin if pr else None,
                "temperature": pr.temperature if pr else None,
                "humidity": pr.humidity if pr else None,
                "roller_temp": pr.roller_temp if pr else None,
                "p_actual": [getattr(pr, f"p_actual_{z}") for z in range(1, 5)] if pr else None,
                "p_init": [getattr(pr, f"p_init_{z}") for z in range(1, 5)] if pr else None,
                "p_set": [getattr(pr, f"p_set_{z}") for z in range(1, 5)] if pr else None,
                "lab_std": [cm.l_std, cm.a_std, cm.b_std] if cm else None,
                "lab_meas": [cm.l_meas, cm.a_meas, cm.b_meas] if cm else None,
                "delta_e": cm.delta_e if cm else None,
                "delta_d": cm.delta_d if cm else None,
                "in_tolerance": bool(cm.in_tolerance) if cm else None,
            })
        return out


@router.get("/history/readings")
def history_readings(
    group_index: int = Query(...),
    minutes: int = Query(120, ge=1, le=60 * 24 * 7),
    step_s: int = Query(60, ge=5, le=3600),
):
    """工况历史曲线（车速/温湿度/各压力模块，分桶平均）。

    Query(...)=必填；ge/le=最小/最大值限制，传超范围参数 FastAPI 自动报 422。
    """
    with Session(get_session_factory().kw["bind"]) as session:
        return bucket_agg(session, PlcReading, minutes, step_s, [
            ("speed", func.avg(PlcReading.speed_mmin)),
            ("temperature", func.avg(PlcReading.temperature)),
            ("humidity", func.avg(PlcReading.humidity)),
            ("roller_temp", func.avg(PlcReading.roller_temp)),
            ("p_actual_1", func.avg(PlcReading.p_actual_1)),
            ("p_actual_2", func.avg(PlcReading.p_actual_2)),
            ("p_actual_3", func.avg(PlcReading.p_actual_3)),
            ("p_actual_4", func.avg(PlcReading.p_actual_4)),
            ("p_init_1", func.avg(PlcReading.p_init_1)),
            ("p_init_2", func.avg(PlcReading.p_init_2)),
            ("p_init_3", func.avg(PlcReading.p_init_3)),
            ("p_init_4", func.avg(PlcReading.p_init_4)),
        ], group_index=group_index)


@router.get("/history/colors")
def history_colors(
    group_index: int = Query(...),
    minutes: int = Query(120, ge=1, le=60 * 24 * 7),
    step_s: int = Query(60, ge=5, le=3600),
):
    """色彩质量历史：ΔE/ΔD/密度均值、桶内最大 ΔE、合格率（0~1）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        return bucket_agg(session, ColorMeasurement, minutes, step_s, [
            ("delta_e", func.avg(ColorMeasurement.delta_e)),
            ("delta_d", func.avg(ColorMeasurement.delta_d)),
            ("delta_e_max", func.max(ColorMeasurement.delta_e)),
            ("density", func.avg(ColorMeasurement.density_meas)),
            ("tol_rate", func.avg(ColorMeasurement.in_tolerance)),
        ], group_index=group_index)


@router.get("/quality/report")
def quality_report(hours: int = Query(24, ge=1, le=720)):
    """质量报表：各机组 ΔE 统计与合格率。

    也是 Text2SQL 的典型问题形态——智能体被问到"过去 24 小时哪个色组质量最差"
    时，生成的 SQL 就是这种 GROUP BY 聚合。
    """
    since = datetime.now() - timedelta(hours=hours)
    with Session(get_session_factory().kw["bind"]) as session:
        rows = session.execute(
            select(
                ColorMeasurement.group_index,
                func.count().label("samples"),
                func.avg(ColorMeasurement.delta_e).label("delta_e_avg"),
                func.max(ColorMeasurement.delta_e).label("delta_e_max"),
                func.avg(ColorMeasurement.delta_d).label("delta_d_avg"),
                func.avg(ColorMeasurement.in_tolerance).label("tol_rate"),
            )
            .where(ColorMeasurement.ts >= since)
            .group_by(ColorMeasurement.group_index)
            .order_by(ColorMeasurement.group_index)
        ).mappings().all()
        return [dict(r) for r in rows]


@router.get("/alarms")
def list_alarms(limit: int = Query(100, ge=1, le=1000)):
    """最新告警列表（采集端只在合格/超差状态翻转时写一条，所以量很小）。"""
    with Session(get_session_factory().kw["bind"]) as session:
        rows = session.execute(
            select(Alarm).order_by(Alarm.ts.desc()).limit(limit)
        ).scalars().all()
        return [_row_dict(a) for a in rows]


@router.get("/collect/status")
def collect_status(hours: int = Query(1, ge=1, le=72)):
    """采集服务健康：近 N 小时各状态批次数与平均耗时（7x24 稳定性口径）。"""
    since = datetime.now() - timedelta(hours=hours)
    with Session(get_session_factory().kw["bind"]) as session:
        rows = session.execute(
            select(
                CollectBatch.status,
                func.count().label("batches"),
                func.avg(CollectBatch.duration_ms).label("avg_ms"),
            )
            .where(CollectBatch.started_at >= since)
            .group_by(CollectBatch.status)
        ).mappings().all()
        return [dict(r) for r in rows]
