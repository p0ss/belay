"""Statistics per cell, the CSV summary and the difference-vs-off table."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SETTING_ORDER = ("off", "proxy", "full")


def percentile(values: List[float], q: float) -> Optional[float]:
    """Linear interpolation between closest ranks (numpy's default)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def stats(values: Iterable[Optional[float]]) -> Dict[str, Optional[float]]:
    xs = [v for v in values if v is not None]
    return {
        "n": len(xs),
        "median": percentile(xs, 0.5),
        "p95": percentile(xs, 0.95),
        "mean": sum(xs) / len(xs) if xs else None,
        "max": max(xs) if xs else None,
    }


def summarise_cell(results: List[dict], wall_s: float, gpu_peak_mb: Optional[float],
                   stream_missing: Optional[int]) -> Dict[str, Any]:
    ok = [r for r in results if "error" not in r]
    alerts = [a for r in ok for a in r.get("alerts", [])]
    tokens = sum(r["tokens"] for r in ok)
    profiles = sorted({r["profile"] for r in ok if r.get("profile")})
    return {
        "requests": len(results),
        "errors": len(results) - len(ok),
        "tokens_total": tokens,
        "wall_s": wall_s,
        "throughput_tok_s": tokens / wall_s if wall_s > 0 else None,
        "requests_per_s": len(ok) / wall_s if wall_s > 0 else None,
        "ms_per_token": stats(r.get("ms_per_token") for r in ok),
        "latency_ms": stats(r["latency_ms"] for r in ok),
        "tokens_per_request": stats(r["tokens"] for r in ok),
        "overhead_ms": stats(r["overhead_ms"] for r in ok),
        "overhead_ms_per_token": stats(r["overhead_ms"] / r["tokens"] for r in ok if r["tokens"]),
        "resident_peak": stats(r["resident_peak"] for r in ok),
        "watched": stats(r["watched"] for r in ok),
        "profiles": profiles,
        "gpu_peak_mb": gpu_peak_mb,
        "alerts": {
            "count": len(alerts),
            "requests_with_alerts": sum(1 for r in ok if r.get("alerts")),
            "signed_latency_ms": stats(a["signed_latency_ms"] for a in alerts),
            "stream_latency_ms": stats(a.get("stream_latency_ms") for a in alerts),
            "stream_missing": stream_missing,
        },
        "signature_failures": {
            "responses": sum(1 for r in ok if not r.get("signature_ok")),
            "stream_records": sum(1 for a in alerts if a.get("stream_signature_ok") is False),
        },
    }


def _key(cell: dict):
    s = cell["summary"]
    order = SETTING_ORDER.index(s["setting"]) if s["setting"] in SETTING_ORDER else len(SETTING_ORDER)
    return (order, s["setting"], s["sessions"])


def sort_cells(cells: List[dict]) -> List[dict]:
    return sorted(cells, key=_key)


def diff_vs_off(cells: List[dict], baseline: str = "off") -> List[dict]:
    """Each non-baseline cell against the baseline cell with the same session count."""
    base = {c["summary"]["sessions"]: c["summary"] for c in cells if c["summary"]["setting"] == baseline}
    rows = []
    for c in sort_cells(cells):
        s = c["summary"]
        b = base.get(s["sessions"])
        if s["setting"] == baseline or b is None:
            continue
        row: Dict[str, Any] = {"setting": s["setting"], "sessions": s["sessions"]}
        for name, get in [
            ("ms_per_token_median", lambda m: m["ms_per_token"]["median"]),
            ("ms_per_token_p95", lambda m: m["ms_per_token"]["p95"]),
            ("latency_ms_median", lambda m: m["latency_ms"]["median"]),
            ("throughput_tok_s", lambda m: m["throughput_tok_s"]),
            ("gpu_peak_mb", lambda m: m["gpu_peak_mb"]),
        ]:
            x, y = get(s), get(b)
            row[f"{name}_off"] = y
            row[f"{name}"] = x
            row[f"{name}_diff"] = None if x is None or y is None else x - y
            row[f"{name}_diff_pct"] = None if x is None or not y else (x - y) / y * 100
        rows.append(row)
    return rows


CSV_COLUMNS = [
    ("setting", lambda s: s["setting"]),
    ("sessions", lambda s: s["sessions"]),
    ("requests", lambda s: s["requests"]),
    ("errors", lambda s: s["errors"]),
    ("tokens_total", lambda s: s["tokens_total"]),
    ("wall_s", lambda s: s["wall_s"]),
    ("throughput_tok_s", lambda s: s["throughput_tok_s"]),
    ("ms_per_token_median", lambda s: s["ms_per_token"]["median"]),
    ("ms_per_token_p95", lambda s: s["ms_per_token"]["p95"]),
    ("latency_ms_median", lambda s: s["latency_ms"]["median"]),
    ("latency_ms_p95", lambda s: s["latency_ms"]["p95"]),
    ("overhead_ms_median", lambda s: s["overhead_ms"]["median"]),
    ("overhead_ms_p95", lambda s: s["overhead_ms"]["p95"]),
    ("overhead_ms_per_token_median", lambda s: s["overhead_ms_per_token"]["median"]),
    ("resident_peak_median", lambda s: s["resident_peak"]["median"]),
    ("resident_peak_max", lambda s: s["resident_peak"]["max"]),
    ("watched_max", lambda s: s["watched"]["max"]),
    ("gpu_peak_mb", lambda s: s["gpu_peak_mb"]),
    ("alerts", lambda s: s["alerts"]["count"]),
    ("alert_signed_latency_ms_median", lambda s: s["alerts"]["signed_latency_ms"]["median"]),
    ("alert_signed_latency_ms_p95", lambda s: s["alerts"]["signed_latency_ms"]["p95"]),
    ("alert_stream_latency_ms_median", lambda s: s["alerts"]["stream_latency_ms"]["median"]),
    ("alert_stream_latency_ms_p95", lambda s: s["alerts"]["stream_latency_ms"]["p95"]),
    ("alert_stream_missing", lambda s: s["alerts"]["stream_missing"]),
    ("bad_response_signatures", lambda s: s["signature_failures"]["responses"]),
    ("bad_stream_signatures", lambda s: s["signature_failures"]["stream_records"]),
]


def _round(x: Any) -> Any:
    return round(x, 3) if isinstance(x, float) else x


def write_csv(cells: List[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([name for name, _ in CSV_COLUMNS])
        for c in sort_cells(cells):
            w.writerow([_round(get(c["summary"])) for _, get in CSV_COLUMNS])


def write_diff_csv(rows: List[dict], path: Path) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for row in rows:
            w.writerow({k: _round(v) for k, v in row.items()})


def write_outputs(results: dict, out_dir: Path, stem: str) -> Dict[str, Path]:
    """Write <stem>.json, <stem>.csv and, when off is present, <stem>-diff.csv."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results["cells"] = sort_cells(results["cells"])
    results["diff_vs_off"] = diff_vs_off(results["cells"])
    paths = {"json": out_dir / f"{stem}.json", "csv": out_dir / f"{stem}.csv"}
    paths["json"].write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csv(results["cells"], paths["csv"])
    if results["diff_vs_off"]:
        paths["diff_csv"] = out_dir / f"{stem}-diff.csv"
        write_diff_csv(results["diff_vs_off"], paths["diff_csv"])
    return paths


def _f(x: Optional[float], spec: str = ".2f") -> str:
    return "-" if x is None else format(x, spec)


def format_tables(results: dict) -> str:
    lines = ["setting  sessions  ms/tok med   p95  latency med ms  tok/s  resident  overhead ms  "
             "alert signed med/p95 ms  alert stream med/p95 ms  bad sig"]
    for c in results["cells"]:
        s = c["summary"]
        a = s["alerts"]
        lines.append(
            f"{s['setting']:<8} {s['sessions']:>8}  {_f(s['ms_per_token']['median']):>10} "
            f"{_f(s['ms_per_token']['p95']):>5}  {_f(s['latency_ms']['median'], '.1f'):>14} "
            f"{_f(s['throughput_tok_s'], '.1f'):>6}  {_f(s['resident_peak']['max'], '.0f'):>8}  "
            f"{_f(s['overhead_ms']['median']):>11}  "
            f"{_f(a['signed_latency_ms']['median'], '.1f'):>11}/{_f(a['signed_latency_ms']['p95'], '.1f'):<11} "
            f"{_f(a['stream_latency_ms']['median'], '.1f'):>11}/{_f(a['stream_latency_ms']['p95'], '.1f'):<11} "
            f"{s['signature_failures']['responses'] + s['signature_failures']['stream_records']:>7}")
    if results.get("diff_vs_off"):
        lines += ["", "difference vs off      ms/tok med (diff, %)        tok/s (diff, %)"]
        for d in results["diff_vs_off"]:
            lines.append(
                f"{d['setting']:<8} {d['sessions']:>8}  "
                f"{_f(d['ms_per_token_median'])} ({_f(d['ms_per_token_median_diff'], '+.2f')}, "
                f"{_f(d['ms_per_token_median_diff_pct'], '+.1f')}%)   "
                f"{_f(d['throughput_tok_s'], '.1f')} ({_f(d['throughput_tok_s_diff'], '+.1f')}, "
                f"{_f(d['throughput_tok_s_diff_pct'], '+.1f')}%)")
    return "\n".join(lines)
