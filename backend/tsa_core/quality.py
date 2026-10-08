"""Quality codes of measurements (OPC UA status codes + "substituted")."""

from enum import StrEnum


class Quality(StrEnum):
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"
    SUBSTITUTED = "substituted"


def quality_from_source(
    val: float | None,
    nd: bool | None,
    otkl: int | None,
    x_min: float | None = None,
    x_max: float | None = None,
) -> Quality:
    """Derive the quality code from the fields of the source `analog` table.

    `nd` ("недостовірно") marks invalid data, a non-zero `otkl` marks a deviation
    reported by the source system. Values outside the admissible range of the channel
    are kept but marked as uncertain.
    """
    if val is None or val != val or nd:  # val != val -> NaN
        return Quality.BAD
    if otkl:
        return Quality.UNCERTAIN
    if (x_min is not None and val < x_min) or (x_max is not None and val > x_max):
        return Quality.UNCERTAIN
    return Quality.GOOD
