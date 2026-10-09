"""API schemas (Pydantic v2). Timestamps are int64 microseconds since the Unix epoch."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- ingest -------------------------------------------------------------------------


class ChannelBatch(BaseModel):
    """Measurements of one channel in columnar form (mirrors the `analog` table)."""

    id: str = Field(min_length=1, max_length=128)
    ts: list[int]
    val: list[float | None]
    nd: list[bool] | None = None
    otkl: list[int] | None = None

    @model_validator(mode="after")
    def _lengths(self) -> "ChannelBatch":
        n = len(self.ts)
        if len(self.val) != n:
            raise ValueError(f"channel {self.id}: len(val) != len(ts)")
        for name in ("nd", "otkl"):
            col = getattr(self, name)
            if col is not None and len(col) != n:
                raise ValueError(f"channel {self.id}: len({name}) != len(ts)")
        return self


class IngestPacket(BaseModel):
    seq: int | None = None
    source: str = "unknown"
    sent_at: int | None = None  # sender clock, us
    channels: list[ChannelBatch]


class IngestAck(BaseModel):
    type: Literal["ack"] = "ack"
    seq: int | None
    accepted: int
    rejected: int
    reordered: int = 0
    new_channels: list[str] = []
    errors: list[str] = []


# --- registry -----------------------------------------------------------------------


class ChannelIn(BaseModel):
    name: str | None = None
    description: str | None = None
    unit: str | None = None
    x_min: float | None = None
    x_max: float | None = None
    f_nominal_hz: float | None = None
    deadband: float | None = None
    source: str | None = None
    source_field: str | None = None
    group_name: str | None = None
    hampel_window: int | None = Field(None, ge=3, le=1001)
    hampel_kappa: float | None = Field(None, gt=0)
    hampel_min_sigma: float | None = Field(None, ge=0)
    feature_window: int | None = Field(None, ge=2, le=10_000)
    residual_k: float | None = Field(None, gt=0)
    analytics_enabled: bool | None = None


class ChannelOut(ChannelIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    auto_registered: bool = False


class ChannelCreate(ChannelIn):
    id: str = Field(min_length=1, max_length=128)


class ChannelStats(BaseModel):
    id: str
    count: int
    first_ts: int
    last_ts: int


class ChannelState(BaseModel):
    id: str
    last_ts: int | None = None
    last_val: float | None = None
    last_quality: str | None = None


# --- series -------------------------------------------------------------------------


class Series(BaseModel):
    channel: str
    source: Literal["raw", "hot", "agg", "empty"]
    level: int | None = None
    n: int  # N: points in the requested interval
    m: int  # target number of points
    eta: float  # share of removed points
    t: list[int]
    v: list[float | None]
    q: list[str] | None = None  # quality codes (raw / hot sources only)
    # Exact statistics of the interval, computed from raw points or aggregates (not from
    # the downsampled curve). For the aggregate source, edge buckets may extend slightly
    # beyond the interval.
    vmin: float | None = None
    vmax: float | None = None
    mean: float | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    channel_id: str
    kind: str
    ts_start: int
    ts_end: int
    n_points: int
    peak_ts: int
    peak_value: float | None
    score: float | None
    run: str
    model_version: str | None
    details: dict | None = None


class FlagPoint(BaseModel):
    channel: str
    ts: int
    val: float | None
    val_f: float | None
    pred: float | None
    resid: float | None
    thr: float | None
    prob: float | None
    substituted: bool
    anomaly: bool


class SeriesResponse(BaseModel):
    t_from: int
    t_to: int
    width_px: int
    series: list[Series]
    events: list[EventOut] = []
    flags: list[FlagPoint] = []
    alarms: list["AlarmOut"] = []
    # totals in the interval; if larger than the returned lists, only the most significant are shown
    events_total: int = 0
    flags_total: int = 0
    timing_ms: dict[str, float] = {}


class RawPoint(BaseModel):
    ts: int
    val: float | None
    quality: str
    nd: bool
    otkl: int
    substituted: bool = False
    val_f: float | None = None


# --- alarms -------------------------------------------------------------------------


class AlarmRuleIn(BaseModel):
    channel_id: str
    kind: Literal["hihi", "hi", "lo", "lolo", "anomaly"]
    limit: float | None = None
    deadband: float = 0.0
    on_delay_ms: int = 0
    off_delay_ms: int = 0
    min_repeat_ms: int = 0
    priority: int = Field(2, ge=1, le=4)
    enabled: bool = True
    description: str | None = None

    @model_validator(mode="after")
    def _limit(self) -> "AlarmRuleIn":
        if self.kind != "anomaly" and self.limit is None:
            raise ValueError("limit is required for limit alarms")
        return self


class AlarmRuleOut(AlarmRuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class AlarmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    rule_id: int
    channel_id: str
    state: str
    priority: int
    ts_active: int
    ts_return: int | None
    ts_ack: int | None
    acked_by: str | None
    value: float | None


class AlarmLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    alarm_id: int
    rule_id: int
    channel_id: str
    ts: int
    prev_state: str
    new_state: str
    reason: str
    value: float | None
    user: str | None


class AckRequest(BaseModel):
    user: str = "operator"


class ModeChangeIn(BaseModel):
    robot: str = "default"
    mode: str
    ts: int
    details: dict | None = None


class ModeChangeOut(ModeChangeIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class ModelVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    channel_id: str
    kind: str
    version: str
    path: str
    params: dict | None
    metrics: dict | None
    active: bool


class TrainRequest(BaseModel):
    channel: str
    t_from: int | None = None  # us; default: whole archive
    t_to: int | None = None
    trees: int = Field(100, ge=10, le=1000)
    depth: int = Field(6, ge=2, le=16)
    # auto: activate only if the model beats the naive forecast on the test part
    activate: Literal["auto", "always", "never"] = "auto"


class TrainingJobOut(BaseModel):
    id: str
    channel: str
    t_from: int | None
    t_to: int | None
    status: Literal["queued", "running", "done", "error"]
    created_at: float
    finished_at: float | None
    result: dict | None
    error: str | None


# --- analysis preview -------------------------------------------------------------


class AnalysisPreviewRequest(BaseModel):
    channel: str
    t_from: int
    t_to: int
    hampel_window: int | None = Field(None, ge=3, le=1001)
    hampel_kappa: float | None = Field(None, gt=0)
    feature_window: int | None = Field(None, ge=2)
    residual_k: float | None = Field(None, gt=0)
    use_model: bool = True
    model_id: int | None = None  # a specific model version (also inactive); default: the active one


class AnalysisPreviewResponse(BaseModel):
    channel: str
    n: int
    substituted: int
    anomalies: int
    model: str
    episodes: list[dict]
    flags: list[FlagPoint]
    elapsed_ms: float
    # one-step-ahead forecast over the interval (downsampled for drawing) and its accuracy
    forecast_t: list[int] = []
    forecast_v: list[float] = []
    mae_model: float | None = None
    mae_naive: float | None = None


# --- live subscription (WebSocket /ws/stream) ----------------------------------


class Subscribe(BaseModel):
    op: Literal["subscribe"]
    channels: list[str] = Field(min_length=1, max_length=64)
    window_s: float = Field(60, gt=0)
    width_px: int = Field(1200, ge=10)
    c: float | None = Field(None, gt=0, le=10)


class Unsubscribe(BaseModel):
    op: Literal["unsubscribe"]


class Ping(BaseModel):
    op: Literal["ping"]
    t: int | None = None


SeriesResponse.model_rebuild()
