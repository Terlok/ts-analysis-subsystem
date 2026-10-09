"""Alarm life cycle according to ANSI/ISA-18.2 (ФВ-3.4).

States: NORMAL, UNACK_ACTIVE (active, unacknowledged), ACK_ACTIVE (active,
acknowledged), UNACK_RTN (returned to normal, unacknowledged).
Chattering is limited by hysteresis (deadband), on/off delays and a minimal
interval between repeated activations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AlarmState(StrEnum):
    NORMAL = "normal"
    UNACK_ACTIVE = "unack_active"
    ACK_ACTIVE = "ack_active"
    UNACK_RTN = "unack_rtn"


class AlarmKind(StrEnum):
    HIHI = "hihi"
    HI = "hi"
    LO = "lo"
    LOLO = "lolo"
    ANOMALY = "anomaly"  # driven by the combined anomaly criterion


@dataclass
class AlarmRule:
    id: int
    channel: str
    kind: AlarmKind
    limit: float | None = None
    deadband: float = 0.0
    on_delay_us: int = 0
    off_delay_us: int = 0
    min_repeat_us: int = 0
    priority: int = 2
    enabled: bool = True


@dataclass
class AlarmTransition:
    rule_id: int
    channel: str
    ts: int
    prev: AlarmState
    new: AlarmState
    value: float | None
    reason: str  # activated | returned | acknowledged


@dataclass
class AlarmRuntime:
    state: AlarmState = AlarmState.NORMAL
    condition: bool = False  # debounced condition
    raw_since: int | None = None  # when the raw condition changed to differ from `condition`
    last_activation: int | None = None


def _raw_condition(rule: AlarmRule, value: float, anomaly: bool, currently_active: bool) -> bool:
    if rule.kind == AlarmKind.ANOMALY:
        return anomaly
    if value != value or rule.limit is None:
        return currently_active
    db = rule.deadband if currently_active else 0.0
    if rule.kind in (AlarmKind.HI, AlarmKind.HIHI):
        return value > rule.limit - db
    return value < rule.limit + db


def evaluate(rule: AlarmRule, rt: AlarmRuntime, ts: int, value: float, anomaly: bool = False) -> AlarmTransition | None:
    """Feed one sample; mutates `rt` and returns a state transition if one happened."""
    if not rule.enabled:
        return None
    raw = _raw_condition(rule, value, anomaly, rt.condition)
    if raw == rt.condition:
        rt.raw_since = None
        return None
    if rt.raw_since is None:
        rt.raw_since = ts
    delay = rule.on_delay_us if raw else rule.off_delay_us
    if ts - rt.raw_since < delay:
        return None
    if raw and rt.last_activation is not None and ts - rt.last_activation < rule.min_repeat_us:
        return None  # repeated activation suppressed
    rt.condition = raw
    rt.raw_since = None

    prev = rt.state
    if raw:
        rt.last_activation = ts
        new = AlarmState.UNACK_ACTIVE if prev in (AlarmState.NORMAL, AlarmState.UNACK_RTN) else prev
        reason = "activated"
    else:
        if prev == AlarmState.UNACK_ACTIVE:
            new = AlarmState.UNACK_RTN
        elif prev == AlarmState.ACK_ACTIVE:
            new = AlarmState.NORMAL
        else:
            new = prev
        reason = "returned"
    if new == prev:
        return None
    rt.state = new
    return AlarmTransition(rule.id, rule.channel, ts, prev, new, None if value != value else value, reason)


def acknowledge(rule: AlarmRule, rt: AlarmRuntime, ts: int) -> AlarmTransition | None:
    prev = rt.state
    if prev == AlarmState.UNACK_ACTIVE:
        rt.state = AlarmState.ACK_ACTIVE
    elif prev == AlarmState.UNACK_RTN:
        rt.state = AlarmState.NORMAL
    else:
        return None
    return AlarmTransition(rule.id, rule.channel, ts, prev, rt.state, None, "acknowledged")
