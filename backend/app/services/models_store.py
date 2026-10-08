"""Loading of the active diagnostic models of channels (registered in `model_versions`)."""

from __future__ import annotations

import logging
from pathlib import Path

from app.db.models import ModelVersion
from tsa_core.detectors import Classifier, Forecaster, load_model

log = logging.getLogger(__name__)


def load_active(rows: list[ModelVersion], models_dir: str) -> dict[str, tuple[Forecaster | None, Classifier | None]]:
    """rows: active ModelVersion records -> {channel: (forecaster, classifier)}."""
    out: dict[str, list] = {}
    for r in rows:
        path = Path(r.path)
        if not path.is_absolute():
            path = Path(models_dir) / path
        try:
            model = load_model(path)
        except Exception:  # noqa: BLE001 - a broken model must not stop the pipeline
            log.exception("cannot load model %s for %s", path, r.channel_id)
            continue
        slot = out.setdefault(r.channel_id, [None, None])
        slot[0 if r.kind == "forecaster" else 1] = model
    return {k: (v[0], v[1]) for k, v in out.items()}
