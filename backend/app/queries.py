"""常用 ORM 查询（routes_data / routes_model 共用的查询工具函数）。

【零基础说明】
写接口时经常遇到两类"有点绕"的查询，抽出来放这里，避免复制粘贴：
1. latest_per_group：取"每个机组的最新一条数据"
2. bucket_agg：把密集时序数据按时间桶聚合（画图降采样）
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from db.models import ColorMeasurement, PlcReading


def latest_per_group(session: Session, model, ts_desc: bool = True) -> dict[int, dict]:
    """取每个机组的最新一条记录，返回 {group_index: ORM对象}。

    【为什么不能简单 ORDER BY ts LIMIT 10？】
    那样取到的是"最新的 10 行"，可能全是 0 号机组的（它写入最频繁）。
    我们要的是"每组各一条最新"——标准做法是先算每组的 max(ts)（子查询），
    再拿原表来跟这个结果 JOIN：
        原表 JOIN (SELECT group_index, MAX(ts) FROM 原表 GROUP BY group_index)
        ON 原表.group_index = 子查询.group_index AND 原表.ts = 子查询.max_ts
    """
    maxts = (
        select(model.group_index, func.max(model.ts).label("mts"))
        .group_by(model.group_index)
        .subquery()
    )
    rows = session.execute(
        select(model).join(maxts, and_(
            model.group_index == maxts.c.group_index,
            model.ts == maxts.c.mts,
        ))
    ).scalars().all()
    return {r.group_index: r for r in rows}


def bucket_agg(session: Session, model, minutes: int, step_s: int,
               agg_cols: list, group_index: int | None = None) -> list[dict]:
    """把时序数据按时间桶聚合（ECharts 曲线的数据源）。

    【为什么需要聚合？】
    2 秒一条数据，看 24 小时就是 43200 个点——浏览器画不动，人眼也看不了。
    按 step_s 秒一桶求平均（比如 300 秒=5 分钟），24 小时就只剩 288 个点。

    【SQL 技巧】
    UNIX_TIMESTAMP(ts) 把时间转成秒数，除以 step_s 再取整，
    同一桶的数据落到同一个整数上 -> GROUP BY 它就是分桶：
        FLOOR(UNIX_TIMESTAMP(ts)/300)*300  ->  再 FROM_UNIXTIME 转回时间
    """
    since = datetime.now() - timedelta(minutes=minutes)
    bucket = (func.from_unixtime(func.floor(func.unix_timestamp(model.ts) / step_s) * step_s))
    q = select(bucket.label("bucket"),
               *[fn.label(name) for name, fn in agg_cols]) \
        .where(model.ts >= since)
    if group_index is not None:
        q = q.where(model.group_index == group_index)
    q = q.group_by(bucket).order_by(bucket)
    rows = session.execute(q).mappings().all()
    return [dict(r) for r in rows]
