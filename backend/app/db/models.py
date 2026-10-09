"""PostgreSQL schema: channel registry, alarms, events, model versions, robot modes (ФВ-2.4).

Every event references primary data in QuestDB by (channel, ts) with ts in
microseconds (BIGINT, same unit as QuestDB). The references cross the boundary of
two DBMS, so their integrity is maintained by the application, not by foreign keys.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Channel(Base):
    """Tag: named process variable with metadata M_i = <id, u, [x_min, x_max], f, d, Lambda>."""

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(32))
    x_min: Mapped[float | None] = mapped_column(Double)
    x_max: Mapped[float | None] = mapped_column(Double)
    f_nominal_hz: Mapped[float | None] = mapped_column(Double)
    deadband: Mapped[float | None] = mapped_column(Double)
    # Source in the robot middleware (e.g. ROS 2 topic and message field) or source table
    source: Mapped[str | None] = mapped_column(String(256))
    source_field: Mapped[str | None] = mapped_column(String(256))
    group_name: Mapped[str | None] = mapped_column(String(128))
    # Per-channel overrides of analytics parameters (NULL -> global defaults)
    hampel_window: Mapped[int | None] = mapped_column(Integer)
    hampel_kappa: Mapped[float | None] = mapped_column(Double)
    hampel_min_sigma: Mapped[float | None] = mapped_column(Double)
    feature_window: Mapped[int | None] = mapped_column(Integer)
    residual_k: Mapped[float | None] = mapped_column(Double)
    analytics_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    auto_registered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AlarmRule(Base):
    __tablename__ = "alarm_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # hihi | hi | lo | lolo | anomaly
    limit: Mapped[float | None] = mapped_column(Double)
    deadband: Mapped[float] = mapped_column(Double, default=0.0, server_default="0")
    on_delay_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    off_delay_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    min_repeat_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    priority: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alarm(Base):
    """One occurrence of an alarm (from activation until it is normal again)."""

    __tablename__ = "alarms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alarm_rules.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    priority: Mapped[int] = mapped_column(Integer)
    ts_active: Mapped[int] = mapped_column(BigInteger)  # us, reference into QuestDB
    ts_return: Mapped[int | None] = mapped_column(BigInteger)
    ts_ack: Mapped[int | None] = mapped_column(BigInteger)
    acked_by: Mapped[str | None] = mapped_column(String(128))
    value: Mapped[float | None] = mapped_column(Double)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("ix_alarms_channel_ts", "channel_id", "ts_active"),)


class AlarmLog(Base):
    """Journal of alarm state transitions."""

    __tablename__ = "alarm_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alarm_id: Mapped[int] = mapped_column(ForeignKey("alarms.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[str] = mapped_column(String(128))
    ts: Mapped[int] = mapped_column(BigInteger)
    prev_state: Mapped[str] = mapped_column(String(16))
    new_state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(32))
    value: Mapped[float | None] = mapped_column(Double)
    user: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    """Detected anomaly / substituted-outlier episode with a reference into the archive."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # anomaly | outlier
    ts_start: Mapped[int] = mapped_column(BigInteger)
    ts_end: Mapped[int] = mapped_column(BigInteger)
    n_points: Mapped[int] = mapped_column(Integer)
    peak_ts: Mapped[int] = mapped_column(BigInteger)
    peak_value: Mapped[float | None] = mapped_column(Double)
    score: Mapped[float | None] = mapped_column(Double)
    run: Mapped[str] = mapped_column(String(64), default="online", server_default="online")
    model_version: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_events_channel_ts", "channel_id", "ts_start"), Index("ix_events_run", "run"))


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # forecaster | classifier
    version: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    params: Mapped[dict | None] = mapped_column(JSONB)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModeChange(Base):
    """Robot operating mode changes (manual / automatic / emergency, task phase)."""

    __tablename__ = "mode_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    robot: Mapped[str] = mapped_column(String(128), default="default", server_default="default")
    mode: Mapped[str] = mapped_column(String(64))
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    details: Mapped[dict | None] = mapped_column(JSONB)
