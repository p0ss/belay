"""
belay-bench: the overhead matrix (docs/evaluation.md, sections 1 and 2).

    # one setting against a detector already running (the default)
    belay-bench run --url http://127.0.0.1:8701 --setting proxy

    # every setting, launching and stopping the detector for each
    belay-bench run --launch --settings off,proxy,full

    # every setting against the in-process stub: no model, no GPU
    belay-bench run --dry-run

    # merge separate --url runs into one results file with the diff-vs-off table
    belay-bench summarize runs/bench/bench-*-off.json runs/bench/bench-*-proxy.json ...
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from belay.contract import key_from_env
from belay.contract.signing import LAB_KEY_ENV

from .launch import DEFAULT_COMMAND, launched, serve_in_thread, wait_healthy
from .monitors import AlertWatcher, GpuPoller
from .report import format_tables, write_outputs
from .runner import PROMPTS_PATH, Detector, load_prompts, run_setting

SETTINGS = ("off", "proxy", "full")


def _ints(text: str) -> List[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def _strs(text: str) -> List[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _stub_detector(latency_ms: float, log_path: Path) -> Iterator[str]:
    from belay.contract.service import create_app
    from belay.contract.stub import StubBackend

    with serve_in_thread(create_app(StubBackend(latency_ms=latency_ms), log_path)) as url:
        yield url


def run(args: argparse.Namespace) -> dict:
    key = key_from_env()
    prompts_path = Path(args.prompts)
    prompts = load_prompts(prompts_path)
    run_id = args.run_id or dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = Path(args.out)

    if args.dry_run:
        mode, settings = "dry-run", _strs(args.settings)
    elif args.launch:
        mode, settings = "launch", _strs(args.settings)
    else:
        if not args.setting:
            sys.exit("belay-bench run: --setting is required with --url (the label for the detector's watch setting)")
        mode, settings = "url", [args.setting]

    gpu = GpuPoller(interval_s=args.gpu_interval, gpu_index=args.gpu_index, enabled=not args.no_gpu and mode != "dry-run")
    gpu.start()
    results = {
        "run_id": run_id,
        "mode": mode,
        "started": _now(),
        "settings": settings,
        "session_counts": _ints(args.sessions),
        "requests_per_cell": args.requests,
        "warmup_per_session": args.warmup,
        "max_tokens": args.max_tokens,
        "prompts": {"path": str(prompts_path), "count": len(prompts),
                    "sha256": hashlib.sha256(prompts_path.read_bytes()).hexdigest()},
        "lab_key": "env" if os.environ.get(LAB_KEY_ENV) else "dev-default",
        "host": {"platform": platform.platform(), "python": platform.python_version(),
                 "node": platform.node()},
        "gpu": {"available": gpu.available, "idle_mb": gpu.sample() if gpu.available else None},
        "detectors": {},
        "cells": [],
    }
    log = (lambda *_: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))

    for setting in settings:
        if mode == "dry-run":
            ctx = _stub_detector(args.stub_latency_ms, out_dir / f"bench-{run_id}-{setting}-alerts.jsonl")
        elif mode == "launch":
            ctx = launched(args.command, setting, args.host, args.port,
                           out_dir / f"bench-{run_id}-{setting}.log", args.startup_timeout)
        else:
            ctx = _existing(args.url, args.startup_timeout)
        with ctx as url:
            health = wait_healthy(url, args.startup_timeout)
            det = Detector(url=url, key=key, gpu=gpu, identity=health.get("identity", {}))
            info = {"url": url, "identity": det.identity, "gpu_loaded_mb": gpu.sample() if gpu.available else None}
            if not args.no_stream:
                det.watcher = AlertWatcher(url, key).start()
            try:
                cells = run_setting(det, setting, results["session_counts"], args.requests, run_id, prompts,
                                    warmup=args.warmup, max_tokens=args.max_tokens, timeout_s=args.timeout,
                                    stream_grace_s=args.stream_grace, log=log)
            finally:
                if det.watcher is not None:
                    info["stream"] = {"records": det.watcher.records, "bad_signatures": det.watcher.bad_signatures,
                                      "error": det.watcher.error}
                    det.watcher.stop()
            results["detectors"][setting] = info
            results["cells"] += cells

    gpu.stop()
    results["finished"] = _now()
    stem = f"bench-{run_id}" + (f"-{settings[0]}" if mode == "url" else "")
    paths = write_outputs(results, out_dir, stem)
    if not args.quiet:
        print(format_tables(results))
        for name, p in paths.items():
            print(f"[bench] wrote {name}: {p}")
    results["_paths"] = {k: str(v) for k, v in paths.items()}
    return results


@contextmanager
def _existing(url: str, timeout_s: float) -> Iterator[str]:
    wait_healthy(url, timeout_s)
    yield url


def summarize(args: argparse.Namespace) -> dict:
    sources = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.files]
    run_id = args.run_id or "merged-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    merged = {
        "run_id": run_id,
        "mode": "merged",
        "started": min(s["started"] for s in sources),
        "finished": max(s.get("finished", s["started"]) for s in sources),
        "sources": [{"file": str(p), "run_id": s["run_id"], "mode": s["mode"], "settings": s["settings"],
                     "prompts_sha256": s["prompts"]["sha256"]} for p, s in zip(args.files, sources)],
        "settings": [x for s in sources for x in s["settings"]],
        "detectors": {k: v for s in sources for k, v in s.get("detectors", {}).items()},
        "gpu": [s.get("gpu") for s in sources],
        "cells": [c for s in sources for c in s["cells"]],
    }
    if len({s["prompts"]["sha256"] for s in sources}) > 1:
        print("[bench] warning: the runs used different prompt sets", file=sys.stderr)
    paths = write_outputs(merged, Path(args.out), f"bench-{run_id}")
    print(format_tables(merged))
    for name, p in paths.items():
        print(f"[bench] wrote {name}: {p}")
    return merged


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="belay-bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the matrix")
    where = r.add_mutually_exclusive_group()
    where.add_argument("--url", default="http://127.0.0.1:8701", help="a detector already running (default)")
    where.add_argument("--launch", action="store_true", help="start and stop the detector for each setting")
    where.add_argument("--dry-run", action="store_true", help="use the in-process stub detector for each setting")
    r.add_argument("--setting", choices=SETTINGS, help="label for the detector at --url (its --watch flag)")
    r.add_argument("--settings", default=",".join(SETTINGS), help="with --launch or --dry-run (default off,proxy,full)")
    r.add_argument("--command", default=DEFAULT_COMMAND,
                   help="with --launch: command template, formatted with {setting}, {port}, {host}")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8701)
    r.add_argument("--startup-timeout", type=float, default=900.0, help="seconds to wait for /health")
    r.add_argument("--sessions", default="1,4,8,16", help="concurrent session counts")
    r.add_argument("--requests", type=int, default=64, help="measured requests per cell (at least one per session)")
    r.add_argument("--warmup", type=int, default=1, help="warm-up requests per session, excluded")
    r.add_argument("--max-tokens", type=int, default=128)
    r.add_argument("--timeout", type=float, default=600.0, help="per-request timeout, seconds")
    r.add_argument("--stream-grace", type=float, default=5.0,
                   help="seconds to wait for a cell's alerts to appear on GET /alerts")
    r.add_argument("--no-stream", action="store_true", help="do not follow GET /alerts")
    r.add_argument("--no-gpu", action="store_true", help="do not poll nvidia-smi")
    r.add_argument("--gpu-index", type=int, default=None)
    r.add_argument("--gpu-interval", type=float, default=0.1, help="nvidia-smi poll interval, seconds")
    r.add_argument("--stub-latency-ms", type=float, default=20.0, help="with --dry-run")
    r.add_argument("--prompts", default=str(PROMPTS_PATH))
    r.add_argument("--out", default="runs/bench")
    r.add_argument("--run-id", default=None)
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(func=run)

    s = sub.add_parser("summarize", help="merge results files (e.g. one per --url setting)")
    s.add_argument("files", nargs="+")
    s.add_argument("--out", default="runs/bench")
    s.add_argument("--run-id", default=None)
    s.set_defaults(func=summarize)
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
