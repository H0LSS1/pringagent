"""初始化数据库：ORM 建表 + 种子数据（设备/机组/点位）。

【零基础说明：什么是"种子数据"？】
系统运行依赖一些基础配置数据（有哪些设备、有哪些机组、每个点位在哪）。
init_db 负责在第一次启动时把这些"起点数据"写进库，之后服务才能正常运转。

用法（backend 目录下）：
    .venv\\Scripts\\python -m db.init_db [--groups 10]

幂等性：重复执行不会产生重复数据——
  - 建表：CREATE IF NOT EXISTS 语义（create_all 默认跳过已有表）
  - 设备：INSERT ... ON DUPLICATE KEY UPDATE（有则更新，无则插入）
  - 机组/点位：先删后插（这两张表是纯配置，采集过程不引用其 id 做外键）
"""

import argparse

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from config import settings
from db.models import Device, DeviceGroup, Point
from db.mysql import get_session_factory, init_schema
from register_map import build_point_templates
from simulator.physics import PALETTE

# (设备编码, 名称, 类型, Modbus unit id)——必须与模拟器/register_map 一致
DEVICE_SEEDS = [
    ("PLC-01", "印刷机 PLC", "plc", 1),
    ("SPECTRO-01", "在线分光光度计", "spectro", 2),
]


def seed(n_groups: int) -> None:
    """写入设备/机组/点位三类种子数据。"""
    with Session(get_session_factory().kw["bind"]) as session:
        # ---- 设备表：MySQL 方言的 upsert ----
        # stmt.inserted.name 表示"冲突时更新成这次想插入的新值"
        for code, name, typ, unit in DEVICE_SEEDS:
            stmt = mysql_insert(Device).values(
                code=code, name=name, type=typ,
                host=settings.modbus_host, port=settings.modbus_port, unit_id=unit,
            )
            stmt = stmt.on_duplicate_key_update(
                name=stmt.inserted.name,
                host=stmt.inserted.host,
                port=stmt.inserted.port,
                unit_id=stmt.inserted.unit_id,
            )
            session.execute(stmt)

        dev_ids = dict(session.execute(select(Device.code, Device.id)).all())

        # ---- 机组表：全量重建 ----
        # 机组命名带上色板颜色（1号色组-青），方便前端展示和智能体回答
        session.query(DeviceGroup).delete()
        session.query(Point).delete()
        for code, _name, _typ, _unit in DEVICE_SEEDS:
            session.execute(insert(DeviceGroup), [
                {"device_id": dev_ids[code], "group_index": g,
                 "name": (f"{g + 1}号色组-{PALETTE[g % len(PALETTE)][0]}"
                          if code == "SPECTRO-01" else f"{g + 1}号机组")}
                for g in range(n_groups)
            ])
        # ---- 点位表：写入"寄存器地址表"本身（register_map.build_point_templates）----
        session.execute(insert(Point), [
            {"device_id": dev_ids[tpl["device_code"]], "code": tpl["code"],
             "name": tpl["name"], "unit": tpl["unit"], "scope": tpl["scope"],
             "register_addr": tpl["register_addr"], "scale": tpl["scale"],
             "is_signed": bool(tpl["signed"]), "writable": bool(tpl["writable"])}
            for tpl in build_point_templates()
        ])
        session.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=10, help="机组数量（8-12）")
    args = ap.parse_args()

    init_schema()
    seed(args.groups)

    # 打印结果清单，方便人工确认
    with Session(get_session_factory().kw["bind"]) as session:
        n_dev = session.execute(select(func.count()).select_from(Device)).scalar()
        n_grp = session.execute(select(func.count()).select_from(DeviceGroup)).scalar()
        n_pts = session.execute(select(func.count()).select_from(Point)).scalar()
    print(f"[init_db] tables={len(__import__('db.models', fromlist=['Base']).Base.metadata.tables)} "
          f"| devices={n_dev} groups={n_grp} points={n_pts} | db={settings.mysql_db}")


if __name__ == "__main__":
    main()
