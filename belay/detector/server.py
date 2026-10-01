"""
The lab-side detector service.

    uv run belay-detector --port 8701 --watch proxy --tamper none --log runs/detector-alerts.jsonl

Serves the contract's API (POST /generate, GET /alerts, POST /certificate,
GET /health) with belay.contract.service.create_app, which signs, logs and
streams; HatBackend does the generation and monitoring.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from belay.contract.service import create_app

from .backend import (DEFAULT_MODEL, DEFAULT_PACK, DEFAULT_PROFILE, TAMPER_MODES, TAMPER_THRESHOLD,
                      WATCH_MODES, HatBackend)


def main() -> None:
    p = argparse.ArgumentParser(description="Belay lab-side detector (HAT lenses on Gemma 4 E4B-it)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8701)
    p.add_argument("--watch", choices=WATCH_MODES, default="proxy",
                   help="off: no lenses (baseline); proxy: the profile's lenses; full: the whole pack")
    p.add_argument("--tamper", choices=TAMPER_MODES, default="none")
    p.add_argument("--log", default="runs/detector-alerts.jsonl", help="alert log (JSON Lines)")
    p.add_argument("--model", default=os.environ.get("BELAY_MODEL", DEFAULT_MODEL),
                   help="Hugging Face id (must be cached) or a local directory")
    p.add_argument("--pack", type=Path, default=Path(os.environ.get("BELAY_PACK", DEFAULT_PACK)))
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="watch profile for --watch proxy")
    p.add_argument("--threshold", type=float, default=None,
                   help="alert threshold (default: HAT's, 0.99 for a probe-calibrated pack)")
    p.add_argument("--tamper-threshold", type=float, default=TAMPER_THRESHOLD,
                   help="the raised threshold for --tamper threshold")
    p.add_argument("--max-batch", type=int, default=1,
                   help="1: a request queue, one generation at a time; >1: batch waiting requests")
    p.add_argument("--batch-window-ms", type=float, default=5.0,
                   help="how long the worker waits to fill a batch")
    p.add_argument("--device", default="cuda")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn

    backend = HatBackend(
        watch=args.watch, tamper=args.tamper, model=args.model, pack=args.pack, profile=args.profile,
        threshold=args.threshold, tamper_threshold=args.tamper_threshold, max_batch=args.max_batch,
        batch_window_ms=args.batch_window_ms, device=args.device,
    )
    app = create_app(backend, Path(args.log))
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
