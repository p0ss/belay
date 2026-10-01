"""
Runs the overhead matrix against a detector over HTTP: for one watch setting,
each number of concurrent sessions is a cell; each session is a thread with
its own connection, sending requests one after another.

A cell: every session sends its warm-up requests (excluded), all sessions
meet at a barrier, then they share a queue of N measured requests drawn in
order from the committed prompt set, so every setting and session count sees
the same prompts.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from belay.contract import GenerateResponse, verify

from .monitors import AlertWatcher, GpuPoller
from .report import summarise_cell

PROMPTS_PATH = Path(__file__).with_name("prompts.jsonl")


def load_prompts(path: Path = PROMPTS_PATH) -> List[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class CellConfig:
    setting: str
    sessions: int
    requests: int
    warmup: int = 1
    max_tokens: int = 128
    timeout_s: float = 600.0
    stream_grace_s: float = 5.0


@dataclass
class Detector:
    url: str
    key: bytes
    gpu: GpuPoller
    watcher: Optional[AlertWatcher] = None
    identity: Dict[str, Any] = field(default_factory=dict)


def _request(run_id: str, cfg: CellConfig, session: int, n: str, prompt: dict) -> dict:
    return {
        "request_id": f"bench-{run_id}-{cfg.setting}-c{cfg.sessions}-s{session:02d}-{n}",
        "session_id": f"bench-s{session:02d}",
        "agent": f"bench-{session:02d}",
        "messages": [{"role": "user", "content": prompt["text"]}],
        "max_tokens": cfg.max_tokens,
        "episode": f"bench-{run_id}",
    }


def _send(client: httpx.Client, url: str, body: dict, prompt: dict, key: bytes) -> dict:
    rec: Dict[str, Any] = {"request_id": body["request_id"], "session_id": body["session_id"],
                           "prompt_id": prompt["id"], "field": prompt["field"]}
    rec["t_send"] = time.time()
    t0 = time.perf_counter()
    try:
        r = client.post(url + "/generate", json=body)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001 - every failure is counted, none stops the cell
        rec.update(error=f"{type(e).__name__}: {e}", latency_ms=(time.perf_counter() - t0) * 1000)
        return rec
    latency_ms = (time.perf_counter() - t0) * 1000
    rec["t_recv"] = time.time()
    try:
        resp = GenerateResponse.model_validate(data)
        signature_ok = verify(data, key)
    except Exception as e:  # noqa: BLE001
        rec.update(error=f"invalid response: {e}", latency_ms=latency_ms, signature_ok=False)
        return rec
    tokens = resp.tokens
    rec.update(
        latency_ms=latency_ms,
        tokens=tokens,
        ms_per_token=latency_ms / tokens if tokens else None,
        overhead_ms=resp.overhead_ms,
        resident_peak=resp.coverage.resident_peak,
        watched=resp.coverage.watched,
        profile=resp.coverage.profile,
        signature_ok=signature_ok,
        alerts=[{"concept": a.concept, "token_index": a.token_index, "score": a.score,
                 "t_token": a.t_token, "t_signed": a.t_signed,
                 "signed_latency_ms": (a.t_signed - a.t_token) * 1000} for a in resp.alerts],
    )
    return rec


def run_cell(det: Detector, cfg: CellConfig, run_id: str, prompts: List[dict]) -> dict:
    sessions = cfg.sessions
    n_measured = max(cfg.requests, sessions)
    work: "queue.Queue[int]" = queue.Queue()
    for j in range(n_measured):
        work.put(j)
    barrier = threading.Barrier(sessions + 1)
    results: List[dict] = []
    warmups: List[dict] = []
    lock = threading.Lock()
    end_times: List[float] = []

    def session(k: int) -> None:
        with httpx.Client(timeout=cfg.timeout_s) as client:
            for w in range(cfg.warmup):
                prompt = prompts[-(1 + (k * cfg.warmup + w) % len(prompts))]
                rec = _send(client, det.url, _request(run_id, cfg, k, f"w{w}", prompt), prompt, det.key)
                with lock:
                    warmups.append(rec)
            barrier.wait()
            while True:
                try:
                    j = work.get_nowait()
                except queue.Empty:
                    break
                prompt = prompts[j % len(prompts)]
                rec = _send(client, det.url, _request(run_id, cfg, k, f"{j:04d}", prompt), prompt, det.key)
                with lock:
                    results.append(rec)
            with lock:
                end_times.append(time.perf_counter())

    threads = [threading.Thread(target=session, args=(k,), name=f"bench-session-{k}") for k in range(sessions)]
    for t in threads:
        t.start()
    barrier.wait()  # warm-up done in every session
    det.gpu.reset()
    t_start = time.perf_counter()
    for t in threads:
        t.join()
    wall_s = max(end_times) - t_start if end_times else 0.0
    gpu_peak = det.gpu.peak()

    # Alert stream: when did each alert appear, relative to its token?
    stream_missing = None
    if det.watcher is not None:
        keys = [(r["request_id"], a["concept"], a["token_index"]) for r in results for a in r.get("alerts", [])]
        stream_missing = det.watcher.wait_for(keys, cfg.stream_grace_s)
        for r in results:
            for a in r.get("alerts", []):
                hit = det.watcher.lookup((r["request_id"], a["concept"], a["token_index"]))
                a["stream_latency_ms"] = (hit[0] - a["t_token"]) * 1000 if hit else None
                a["stream_signature_ok"] = hit[1] if hit else None

    results.sort(key=lambda r: r["request_id"])
    summary = summarise_cell(results, wall_s=wall_s, gpu_peak_mb=gpu_peak, stream_missing=stream_missing)
    summary.update(setting=cfg.setting, sessions=sessions, warmup_requests=len(warmups),
                   warmup_errors=sum(1 for w in warmups if "error" in w))
    # The setting is only a label we were given: check it against what the detector reports.
    warnings = []
    watched_max = summary["watched"]["max"]
    if cfg.setting == "off" and (watched_max or summary["alerts"]["count"]):
        warnings.append("setting 'off' but the detector reports watched lenses or alerts")
    if cfg.setting != "off" and watched_max == 0:
        warnings.append(f"setting '{cfg.setting}' but the detector reports no watched lenses")
    summary["warnings"] = warnings
    return {"summary": summary, "requests": results}


def run_setting(det: Detector, setting: str, session_counts: List[int], requests: int, run_id: str,
                prompts: List[dict], warmup: int = 1, max_tokens: int = 128, timeout_s: float = 600.0,
                stream_grace_s: float = 5.0, log=print) -> List[dict]:
    cells = []
    for s in session_counts:
        cfg = CellConfig(setting=setting, sessions=s, requests=requests, warmup=warmup,
                         max_tokens=max_tokens, timeout_s=timeout_s, stream_grace_s=stream_grace_s)
        log(f"[bench] {setting} x {s} sessions: {max(requests, s)} requests (+{warmup}/session warm-up)")
        cell = run_cell(det, cfg, run_id, prompts)
        m = cell["summary"]
        log(f"[bench]   ms/token median {_fmt(m['ms_per_token']['median'])}  p95 {_fmt(m['ms_per_token']['p95'])}"
            f"  throughput {_fmt(m['throughput_tok_s'])} tok/s  errors {m['errors']}"
            f"  bad signatures {m['signature_failures']['responses']}")
        for w in m["warnings"]:
            log(f"[bench]   warning: {w}")
        cells.append(cell)
    return cells


def _fmt(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.2f}"
