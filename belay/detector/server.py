"""
The lab-side detector service.

    uv run belay-detector --port 8701 --run wide --report profiles/proxy-redlines.txt \
        --tamper none --log runs/detector-alerts.jsonl

Serves the contract's API (POST /generate, GET /alerts, POST /certificate,
GET /health) with belay.contract.service.create_app, which signs, logs and
streams; HatBackend does the generation and monitoring.

`--run` sets the lenses scored on every token; `--report` the profile whose
alerts cross the boundary. `--watch` is a deprecated alias for `--run`.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from belay.contract.service import create_app

from .backend import (DEFAULT_INTERNAL_LOG, DEFAULT_MODEL, DEFAULT_PACK, DEFAULT_PROFILE, RUN_MODES, TAMPER_MODES,
                      TAMPER_THRESHOLD, WIDE_PACK, HatBackend)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Belay lab-side detector (HAT lenses on Gemma 4 E4B-it)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8701)
    p.add_argument("--run", choices=RUN_MODES, default=None,
                   help="lenses scored on every token. off: none (baseline); proxy: the proxy profile's; "
                        "full: the whole university pack; wide: the First Light pack, dynamically loaded, "
                        "plus the reported lenses (default proxy)")
    p.add_argument("--watch", choices=RUN_MODES, default=None, help="deprecated alias for --run")
    p.add_argument("--report", type=Path, default=None,
                   help="report profile: only its concepts' alerts cross the boundary, and its hash is what "
                        "the identity claims (default: --profile, i.e. profiles/proxy-redlines.txt)")
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="the run profile for --run proxy")
    p.add_argument("--tamper", choices=TAMPER_MODES, default="none")
    p.add_argument("--log", default="runs/detector-alerts.jsonl", help="boundary alert log (JSON Lines)")
    p.add_argument("--internal-log", default=str(DEFAULT_INTERNAL_LOG),
                   help="lab-internal log of non-reported crossings and per-request residency; '' for none")
    p.add_argument("--model", default=os.environ.get("BELAY_MODEL", DEFAULT_MODEL),
                   help="Hugging Face id (must be cached) or a local directory")
    p.add_argument("--pack", type=Path, default=Path(os.environ.get("BELAY_PACK", DEFAULT_PACK)),
                   help="the certified pack the reported lenses come from")
    p.add_argument("--wide-pack", type=Path, default=Path(os.environ.get("BELAY_WIDE_PACK", WIDE_PACK)),
                   help="the pack --run wide loads dynamically")
    p.add_argument("--wide-hierarchy", type=Path, default=None,
                   help="its concept hierarchy (default: bundled, or its source concept pack's)")
    p.add_argument("--wide-top-k", type=int, default=10, help="HAT's top-k for expansion and pruning")
    p.add_argument("--wide-ram-mb", type=int, default=0,
                   help="preload this much of the wide pack into CPU RAM (HAT's tepid cache); 0 for none")
    p.add_argument("--wide-mode", choices=("fused", "dynamic"), default="fused",
                   help="fused: as many lenses as --wide-budget-mb holds, all scored every token in one pass; "
                        "dynamic: HAT's hierarchical loading, a few dozen resident")
    p.add_argument("--wide-budget-mb", type=int, default=2000, help="fused: lens weights to hold on the GPU")
    p.add_argument("--wide-threshold", type=float, default=0.5,
                   help="internal-log threshold for the wide pack (uncalibrated: raw probabilities)")
    p.add_argument("--threshold", type=float, default=None,
                   help="alert threshold (default: HAT's, 0.99 for a probe-calibrated pack)")
    p.add_argument("--tamper-threshold", type=float, default=TAMPER_THRESHOLD,
                   help="the raised threshold for --tamper threshold")
    p.add_argument("--max-batch", type=int, default=16,
                   help="requests generated together as one batch; 1 makes it a plain request queue")
    p.add_argument("--batch-window-ms", type=float, default=5.0,
                   help="how long the worker waits to fill a batch")
    p.add_argument("--device", default="cuda")
    args = p.parse_args(argv)
    if args.run and args.watch and args.run != args.watch:
        p.error("--watch is a deprecated alias for --run; give one")
    args.run = args.run or args.watch or "proxy"
    if args.internal_log and Path(args.internal_log).resolve() == Path(args.log).resolve():
        p.error("the internal log must not be the boundary log")
    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn

    backend = HatBackend(
        run=args.run, tamper=args.tamper, model=args.model, pack=args.pack, profile=args.profile,
        report=args.report, wide_pack=args.wide_pack, wide_hierarchy=args.wide_hierarchy,
        wide_top_k=args.wide_top_k, wide_ram_mb=args.wide_ram_mb, wide_threshold=args.wide_threshold, wide_mode=args.wide_mode, wide_budget_mb=args.wide_budget_mb,
        internal_log=Path(args.internal_log) if args.internal_log else None,
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
