"""Models and off-stream analysis (ФВ-3.5)."""

from __future__ import annotations

import time

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, update

from app.api.deps import get_state
from app.db.models import ModelVersion
from app.db.redis_store import MODELS_VERSION_KEY
from app.schemas import (
    AnalysisPreviewRequest,
    AnalysisPreviewResponse,
    FlagPoint,
    ModelVersionOut,
    TrainingJobOut,
    TrainRequest,
)
from app.services.models_store import load_active
from app.state import AppState
from tsa_core.pipeline import ChannelParams, ChannelProcessor

router = APIRouter(prefix="/api", tags=["analysis"])

MAX_PREVIEW_POINTS = 500_000
MAX_PREVIEW_FLAGS = 5_000


@router.get("/models", response_model=list[ModelVersionOut])
async def list_models(channel: str | None = None, st: AppState = Depends(get_state)):
    q = select(ModelVersion).order_by(ModelVersion.id.desc())
    if channel:
        q = q.where(ModelVersion.channel_id == channel)
    async with st.pg() as s:
        return (await s.execute(q)).scalars().all()


@router.post("/models/{model_id}/activate", response_model=ModelVersionOut)
async def activate_model(model_id: int, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        mv = await s.get(ModelVersion, model_id)
        if mv is None:
            raise HTTPException(404, "model not found")
        await s.execute(
            update(ModelVersion)
            .where(ModelVersion.channel_id == mv.channel_id, ModelVersion.kind == mv.kind)
            .values(active=False)
        )
        mv.active = True
        await s.commit()
        await s.refresh(mv)
    await st.redis.incr(MODELS_VERSION_KEY)  # analytics workers reload models
    return mv


@router.post("/models/train", response_model=TrainingJobOut, status_code=202)
async def train_model(body: TrainRequest, st: AppState = Depends(get_state)):
    """Train the forecasting model of a channel on archived data in a separate process.
    The new version is registered and (by default) activated; analytics workers reload it."""
    if await st.registry.get(body.channel) is None:
        raise HTTPException(404, "channel not found")
    job = st.trainer.submit(body.channel, body.t_from, body.t_to, body.trees, body.depth, body.activate)
    return TrainingJobOut(**job.__dict__)


@router.get("/models/jobs", response_model=list[TrainingJobOut])
async def training_jobs(st: AppState = Depends(get_state)):
    return [TrainingJobOut(**j.__dict__) for j in st.trainer.list()]


@router.post("/analysis/preview", response_model=AnalysisPreviewResponse)
async def analysis_preview(body: AnalysisPreviewRequest, st: AppState = Depends(get_state)):
    """Run the diagnostic pipeline over archived data with given parameters, without
    persisting anything: lets an engineer tune w, kappa, k before applying them."""
    if body.t_to <= body.t_from:
        raise HTTPException(422, "t_to must be greater than t_from")
    raw = await st.qdb.fetch_raw(body.channel, body.t_from, body.t_to, MAX_PREVIEW_POINTS + 1)
    if len(raw) > MAX_PREVIEW_POINTS:
        raise HTTPException(413, f"interval has more than {MAX_PREVIEW_POINTS} points, narrow it")

    meta = await st.registry.get(body.channel)
    d = st.settings.default_channel_params()

    def pick(*vals):
        return next(v for v in vals if v is not None)

    params = ChannelParams(
        hampel_window=pick(body.hampel_window, meta and meta.hampel_window, d.hampel_window),
        hampel_kappa=pick(body.hampel_kappa, meta and meta.hampel_kappa, d.hampel_kappa),
        hampel_min_sigma=pick(meta and meta.hampel_min_sigma, d.hampel_min_sigma),
        feature_window=pick(body.feature_window, meta and meta.feature_window, d.feature_window),
        residual_k=pick(body.residual_k, meta and meta.residual_k, d.residual_k),
        episode_gap_us=d.episode_gap_us,
    )
    forecaster = classifier = None
    if body.use_model:
        async with st.pg() as s:
            rows = (
                await s.execute(
                    select(ModelVersion).where(ModelVersion.channel_id == body.channel, ModelVersion.active.is_(True))
                )
            ).scalars().all()
        models = await run_in_threadpool(load_active, list(rows), st.settings.models_dir)
        forecaster, classifier = models.get(body.channel, (None, None))

    def run():
        t0 = time.perf_counter()
        proc = ChannelProcessor(body.channel, params, forecaster, classifier)
        valid = (raw["quality"] != "bad") & ~np.isnan(raw["val"])
        res = proc.process(raw["ts"], raw["val"], valid)
        episodes = res.episodes + proc.flush()
        return res, episodes, proc.forecaster.version, (time.perf_counter() - t0) * 1000

    res, episodes, model, elapsed = await run_in_threadpool(run)
    idx = np.flatnonzero(res.flagged)[:MAX_PREVIEW_FLAGS]

    def f(a, i):
        v = float(a[i])
        return None if v != v else v

    flags = [
        FlagPoint(
            channel=body.channel, ts=int(res.ts[i]), val=f(res.raw, i), val_f=f(res.filtered, i),
            pred=f(res.pred, i), resid=f(res.resid, i), thr=f(res.threshold, i), prob=f(res.prob, i),
            substituted=bool(res.substituted[i]), anomaly=bool(res.anomaly[i]),
        )
        for i in idx
    ]
    return AnalysisPreviewResponse(
        channel=body.channel,
        n=len(raw),
        substituted=int(res.substituted.sum()),
        anomalies=int(res.anomaly.sum()),
        model=model,
        episodes=[e.__dict__ for e in episodes],
        flags=flags,
        elapsed_ms=round(elapsed, 2),
    )
