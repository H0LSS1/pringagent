"""SQLAlchemy ORM 数据模型（15 张表）。

表结构与关系：
    设备域：Device / DeviceGroup / Point / CollectBatch
    数据域：PlcReading / ColorMeasurement / Alarm
    模型域：ModelRegistry / PressurePrediction / ControlLog
    业务域：User / ChatSession / ChatMessage / EvalCase / KnowledgeChunk
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def ts3() -> DATETIME:
    """毫秒精度 DATETIME(3)，时序数据统一使用。"""
    return DATETIME(fsp=3)


# ============ 设备域 ============

class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, comment="设备编码 PLC-01/SPECTRO-01")
    name: Mapped[str] = mapped_column(String(64), comment="设备名称")
    type: Mapped[str] = mapped_column(Enum("plc", "spectro", name="device_type"), comment="设备类型")
    host: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer)
    unit_id: Mapped[int] = mapped_column(Integer, comment="Modbus unit id")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now)


class DeviceGroup(Base):
    __tablename__ = "device_groups"
    __table_args__ = (UniqueConstraint("device_id", "group_index", name="uq_dev_group"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    group_index: Mapped[int] = mapped_column(Integer, comment="机组序号 0 起")
    name: Mapped[str] = mapped_column(String(64), comment="如 1号色组-青")


class Point(Base):
    __tablename__ = "points"
    __table_args__ = (UniqueConstraint("device_id", "code", name="uq_dev_point"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    code: Mapped[str] = mapped_column(String(64), comment="点位编码")
    name: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(16), default="-")
    scope: Mapped[str] = mapped_column(Enum("global", "group", name="point_scope"),
                                       comment="global=全局地址 group=机组内偏移")
    register_addr: Mapped[int] = mapped_column(Integer,
                                               comment="global: 绝对地址; group: 机组内偏移")
    scale: Mapped[int] = mapped_column(Integer, default=1, comment="工程量换算除数")
    is_signed: Mapped[bool] = mapped_column(Boolean, default=False, comment="int16 补码解析")
    writable: Mapped[bool] = mapped_column(Boolean, default=False)


class CollectBatch(Base):
    __tablename__ = "collect_batches"
    __table_args__ = (Index("idx_cb_started", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(ts3())
    finished_at: Mapped[datetime | None] = mapped_column(ts3(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plc_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    spectro_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(Enum("ok", "partial", "failed", name="batch_status"))
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)


# ============ 数据域 ============

class PlcReading(Base):
    """PLC 压力/工况时序数据（宽表：每机组每周期一行，百万级）。"""
    __tablename__ = "plc_readings"
    __table_args__ = (
        Index("idx_pr_ts", "ts"),
        Index("idx_pr_group_ts", "group_index", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(ts3())
    group_index: Mapped[int] = mapped_column(Integer)
    speed_mmin: Mapped[float] = mapped_column(Numeric(6, 1))
    humidity: Mapped[float] = mapped_column(Numeric(5, 1))
    temperature: Mapped[float] = mapped_column(Numeric(5, 1))
    roller_temp: Mapped[float] = mapped_column(Numeric(5, 1))
    p_actual_1: Mapped[float] = mapped_column(Numeric(6, 2))
    p_actual_2: Mapped[float] = mapped_column(Numeric(6, 2))
    p_actual_3: Mapped[float] = mapped_column(Numeric(6, 2))
    p_actual_4: Mapped[float] = mapped_column(Numeric(6, 2))
    p_init_1: Mapped[float] = mapped_column(Numeric(6, 2), comment="初始压力（人工调试基准，固定）")
    p_init_2: Mapped[float] = mapped_column(Numeric(6, 2))
    p_init_3: Mapped[float] = mapped_column(Numeric(6, 2))
    p_init_4: Mapped[float] = mapped_column(Numeric(6, 2))
    p_set_1: Mapped[float] = mapped_column(Numeric(6, 2))
    p_set_2: Mapped[float] = mapped_column(Numeric(6, 2))
    p_set_3: Mapped[float] = mapped_column(Numeric(6, 2))
    p_set_4: Mapped[float] = mapped_column(Numeric(6, 2))
    status: Mapped[int] = mapped_column(SmallInteger, default=0)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ColorMeasurement(Base):
    """色彩质量时序数据（与 PlcReading 同周期同 ts 时序对齐，百万级）。"""
    __tablename__ = "color_measurements"
    __table_args__ = (
        Index("idx_cm_ts", "ts"),
        Index("idx_cm_group_ts", "group_index", "ts"),
        Index("idx_cm_tol", "ts", "in_tolerance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(ts3())
    group_index: Mapped[int] = mapped_column(Integer)
    l_std: Mapped[float] = mapped_column(Numeric(5, 2))
    a_std: Mapped[float] = mapped_column(Numeric(5, 2))
    b_std: Mapped[float] = mapped_column(Numeric(5, 2))
    l_meas: Mapped[float] = mapped_column(Numeric(5, 2))
    a_meas: Mapped[float] = mapped_column(Numeric(5, 2))
    b_meas: Mapped[float] = mapped_column(Numeric(5, 2))
    delta_e: Mapped[float] = mapped_column(Numeric(5, 2), comment="CIEDE2000")
    delta_d: Mapped[float] = mapped_column(Numeric(6, 2), comment="密度差")
    density_std: Mapped[float] = mapped_column(Numeric(5, 2))
    density_meas: Mapped[float] = mapped_column(Numeric(5, 2))
    in_tolerance: Mapped[bool] = mapped_column(Boolean, default=True)
    measure_seq: Mapped[int] = mapped_column(Integer, default=0)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Alarm(Base):
    __tablename__ = "alarms"
    __table_args__ = (Index("idx_al_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(ts3())
    level: Mapped[str] = mapped_column(Enum("info", "warn", "error", name="alarm_level"),
                                       default="warn")
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(ts3(), nullable=True)


# ============ 模型域 ============

class ModelRegistry(Base):
    __tablename__ = "model_registry"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_model_ver"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    algo: Mapped[str] = mapped_column(String(32))
    feature_names: Mapped[list] = mapped_column(JSON, comment="特征名列表")
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    train_rows: Mapped[int] = mapped_column(Integer, default=0)
    r2: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    mae: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    artifact_path: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now)


class PressurePrediction(Base):
    __tablename__ = "pressure_predictions"
    __table_args__ = (Index("idx_pp_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(ts3())
    model_id: Mapped[int] = mapped_column(Integer)
    group_index: Mapped[int] = mapped_column(Integer)
    zone_index: Mapped[int] = mapped_column(Integer)
    delta_e: Mapped[float] = mapped_column(Numeric(5, 2))
    current_actual: Mapped[float] = mapped_column(Numeric(6, 2))
    current_set: Mapped[float] = mapped_column(Numeric(6, 2))
    predicted_std: Mapped[float] = mapped_column(Numeric(6, 2))
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class ControlLog(Base):
    __tablename__ = "control_logs"
    __table_args__ = (Index("idx_cl_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(ts3())
    source: Mapped[str] = mapped_column(Enum("manual", "model", "agent", name="control_source"),
                                        default="manual")
    operator: Mapped[str] = mapped_column(String(64), default="-")
    group_index: Mapped[int] = mapped_column(Integer)
    zone_index: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32), comment="set_pressure 等")
    register_addr: Mapped[int] = mapped_column(Integer)
    old_value: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    new_value: Mapped[float] = mapped_column(Numeric(6, 2))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result: Mapped[str] = mapped_column(Enum("ok", "failed", "dry_run", name="control_result"),
                                        default="ok")


# ============ 业务域 ============

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(Enum("admin", "engineer", "viewer", name="user_role"),
                                      default="viewer")
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("idx_cs_user", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(128), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("idx_cm_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Enum("user", "assistant", "tool", name="chat_role"))
    content: Mapped[str] = mapped_column(MEDIUMTEXT)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True,
                                              comment="SQL/图表配置/引用条款等结构化输出")
    created_at: Mapped[datetime] = mapped_column(ts3(), default=datetime.now)


class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Enum("rag", "sql", name="eval_kind"))
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now)


class KnowledgeChunk(Base):
    """知识库分块元数据（Milvus 侧向量的 MySQL 影子表，条款溯源用）。"""
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("doc_name", "chunk_index", name="uq_chunk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_name: Mapped[str] = mapped_column(String(255))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(MEDIUMTEXT)
    milvus_pk: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now)
