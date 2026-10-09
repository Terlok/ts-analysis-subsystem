"""Archiver: writes primary measurements from the stream to QuestDB `telemetry_raw` (no decimation).

    python -m workers.archiver
"""

from __future__ import annotations

import logging
import time

from app.db.questdb import IlpWriter
from app.db.redis_store import packet_arrays
from workers.common import LoadMeter, StreamConsumer, settings, setup_logging, worker_args

log = logging.getLogger("archiver")


def main() -> None:
    setup_logging()
    args = worker_args("QuestDB archiver", sharded=False)
    s = settings()
    state = {"writer": IlpWriter(s)}
    meter: LoadMeter | None = None

    def handle(batch):
        t0 = time.perf_counter()
        n = 0
        writer = state["writer"]
        for _id, pkt in batch:
            t_ing = pkt["t_ing"]
            for ch in pkt["ch"]:
                ts, val, q, nd, otkl = packet_arrays(ch)
                writer.telemetry(ch["id"], ts, val, q, nd, otkl, t_ing)
                n += len(ts)
        writer.flush()  # durable before XACK
        meter.record(n, time.perf_counter() - t0)

    def on_error(_e):  # drop the half-sent buffer; pending messages are re-read and re-written
        try:
            state["writer"].sender.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            state["writer"] = IlpWriter(s)
        except Exception:  # noqa: BLE001
            log.warning("QuestDB is still unavailable")

    consumer = StreamConsumer(s, "archiver", args.consumer, handle, count=500, on_error=on_error)
    meter = LoadMeter(consumer.r, f"archiver:{args.consumer}")
    try:
        consumer.run()
    finally:
        state["writer"].close()


if __name__ == "__main__":
    main()
