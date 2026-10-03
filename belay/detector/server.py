"""
The lab-side detector service.

    uv run belay-detector --port 8701 --run wide --report profiles/proxy-redlines.txt \
        --tamper none --log runs/detector-alerts.jsonl

Serves the contract's API (POST /generate, GET /alerts, POST /certificate,
GET /health) with belay.contract.service.create_app, which signs, logs and
streams; HatBackend runs HAT's Monitor and passes on only the reported alerts.

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
    p = argparse.ArgumentParser(description="Belay lab-side detector (HAT's Monitor on Gemma 4 E4B-it)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8701)
    p.add_argument("--run", choices=RUN_MODES, default=None,
                   help="what HAT runs on every token. off: no monitor (baseline); proxy: the university pack "
                        "watching the run profile; full: the same pack watching everything; wide: the First "
                        "Light pack, plus a second HAT Monitor on the university pack for the reported "
                        "concepts (default proxy)")
    p.add_argument("--watch", choices=RUN_MODES, default=None, help="deprecated alias for --run")
    p.add_argument("--report", type=Path, default=None,
                   help="report profile: only its concepts' alerts cross the boundary, and its hash is what "
                        "the identity claims (default: --profile, i.e. profiles/proxy-redlines.txt)")
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                   help="the run profile HAT watches for --run proxy")
    p.add_argument("--tamper", choices=TAMPER_MODES, default="none")
    p.add_argument("--log", default="runs/detector-alerts.jsonl", help="boundary alert log (JSON Lines)")
    p.add_argument("--internal-log", default=str(DEFAULT_INTERNAL_LOG),
                   help="lab-internal log of non-reported alerts and per-request residency; '' for none")
    p.add_argument("--model", default=os.environ.get("BELAY_MODEL", DEFAULT_MODEL),
                   help="Hugging Face id (must be cached) or a local directory")
    p.add_argument("--pack", type=Path, default=Path(os.environ.get("BELAY_PACK", DEFAULT_PACK)),
                   help="the certified pack the reported lenses come from")
    p.add_argument("--wide-pack", type=Path, default=Path(os.environ.get("BELAY_WIDE_PACK", WIDE_PACK)),
                   help="the pack HAT runs for --run wide")
    p.add_argument("--wide-hierarchy", type=Path, default=None,
                   help="its concept hierarchy (default: bundled, or its source concept pack's)")
    p.add_argument("--threshold", type=float, default=None,
                   help="alert threshold of the Monitor whose alerts are reported (default: HAT's, 0.99 for a "
                        "probe-calibrated pack)")
    p.add_argument("--tamper-threshold", type=float, default=TAMPER_THRESHOLD,
                   help="the raised threshold for --tamper threshold")
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
        internal_log=Path(args.internal_log) if args.internal_log else None,
        threshold=args.threshold, tamper_threshold=args.tamper_threshold, device=args.device,
    )
    app = create_app(backend, Path(args.log))
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
