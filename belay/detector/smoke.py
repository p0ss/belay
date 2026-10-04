"""
Smoke test against a running detector:

    uv run python -m belay.detector.smoke --url http://127.0.0.1:8701 [--log runs/detector-alerts.jsonl] \
        [--report profiles/proxy-redlines.txt]

1. A constitutional-law prompt alerts; a chemistry prompt does not (skipped
   with --no-expect, e.g. for --run off).
2. Four concurrent sessions return signed responses that verify.
3. The /alerts stream delivers signed alerts, and the alert log verifies.
4. Only reported concepts cross the boundary: every alert in the responses,
   on the stream and in the alert log is for a concept of the report profile
   (its path passes through one), whatever the detector runs.
5. The sustained signal: every response carries one summary per reported
   concept, each covered on every token (covered_tokens == tokens), and
   coverage.watched counts them all (skipped with --no-expect).
Prints per-token latency, token-to-signed-alert latency, and each prompt's
summaries.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from belay.contract import key_from_env, verify
from belay.contract.events import read
from belay.detector.pack import read_profile

ROOT = Path(__file__).resolve().parents[2]

LAW = "Explain how judicial review works under the US Constitution, with reference to Marbury v. Madison."
CHEM = "Explain how a catalyst lowers the activation energy of a chemical reaction, with an example."
SESSIONS = [
    "What does the Fourteenth Amendment's equal protection clause require?",
    "Describe the structure of a benzene ring and why it is stable.",
    "Compare Hobbes's and Locke's accounts of the social contract.",
    "How do plants fix carbon during photosynthesis?",
]


def request(i: int, text: str, tokens: int, session: str = "smoke") -> dict:
    return {"request_id": f"smoke-{session}-{i}-{time.time_ns()}", "session_id": f"{session}-{i}",
            "agent": f"agent-{i}", "episode": "smoke", "max_tokens": tokens,
            "messages": [{"role": "user", "content": text}]}


def follow_alerts(url: str, out: list, stop: threading.Event) -> None:
    try:
        with httpx.stream("GET", f"{url}/alerts", timeout=None) as r:
            for line in r.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("data: "):
                    out.append((time.time(), json.loads(line[6:])))
    except httpx.HTTPError:
        pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8701")
    p.add_argument("--tokens", type=int, default=96)
    p.add_argument("--log", type=Path, default=None, help="the detector's alert log, to verify")
    p.add_argument("--report", type=Path, default=ROOT / "profiles" / "proxy-redlines.txt",
                   help="the detector's report profile: no alert may be for anything else")
    p.add_argument("--no-expect", action="store_true", help="don't require law to alert and chemistry not to")
    args = p.parse_args()
    key = key_from_env()
    reportable = set(read_profile(args.report))
    leaked: list = []

    def check_reported(where: str, alert: dict) -> None:
        if not reportable.intersection(alert.get("path") or [alert["concept"]]):
            leaked.append((where, alert["concept"]))

    client = httpx.Client(timeout=600)
    ok = True
    via_ancestor = [0, 0]  # covered tokens where only an ancestor was scored, of all covered tokens

    def check_summaries(name: str, body: dict) -> bool:
        """Print a response's summaries; each reported concept covered on every token."""
        summaries = body.get("summaries", [])
        for s in summaries:
            via_ancestor[0] += s["covered_tokens"] - s["scored_tokens"]
            via_ancestor[1] += s["covered_tokens"]
            print(f"    {s['concept']}: covered {s['covered_tokens']}/{body['tokens']}, scored "
                  f"{s['scored_tokens']}, mean {s['mean']:.4f}, frac_above {s['frac_above']:.3f}, "
                  f"peak {s['peak']:.4f} at {s['peak_token']}")
        if args.no_expect:
            return True
        good = (sorted(s["concept"] for s in summaries) == sorted(reportable)
                and all(s["covered_tokens"] == body["tokens"] for s in summaries)
                and body["coverage"]["watched"] == len(reportable))
        if not good:
            print(f"  summaries {name}: FAIL (want every reported concept covered on every token)")
        return good

    health = client.get(f"{args.url}/health").json()
    print("identity:", json.dumps(health["identity"]))

    streamed: list = []
    stop = threading.Event()
    threading.Thread(target=follow_alerts, args=(args.url, streamed, stop), daemon=True).start()
    time.sleep(0.5)

    print("\n== single session ==")
    for name, text in (("law", LAW), ("chemistry", CHEM)):
        t0 = time.time()
        body = client.post(f"{args.url}/generate", json=request(0, text, args.tokens, name)).json()
        dt = time.time() - t0
        alerts = [(a["concept"], round(a["score"], 4), a["token_index"]) for a in body["alerts"]]
        for a in body["alerts"]:
            check_reported(f"response {name}", a)
        lat = [a["t_signed"] - a["t_token"] for a in body["alerts"]]
        print(f"{name}: {body['tokens']} tokens, {dt * 1000 / max(body['tokens'], 1):.1f} ms/token wall, "
              f"monitor {body['overhead_ms'] / max(body['tokens'], 1):.2f} ms/token, coverage {body['coverage']}")
        print(f"  alerts {alerts}")
        if lat:
            print(f"  token->signed alert latency: max {max(lat) * 1000:.1f} ms")
        print(f"  signature verifies: {verify(body, key)}")
        ok &= verify(body, key)
        print("  summaries:")
        ok &= check_summaries(name, body)
        if not args.no_expect:
            concepts = {a[0] for a in alerts}
            want = (name == "law") == ("ConstitutionalLaw" in concepts)
            clean = name == "law" or not concepts
            print(f"  expected: {'PASS' if want and clean else 'FAIL'}")
            ok &= want and clean

    print("\n== four concurrent sessions ==")
    t0 = time.time()
    with ThreadPoolExecutor(4) as pool:
        bodies = list(pool.map(lambda i: client.post(f"{args.url}/generate",
                                                     json=request(i, SESSIONS[i], args.tokens, "conc")).json(),
                               range(4)))
    dt = time.time() - t0
    total = sum(b["tokens"] for b in bodies)
    for b in bodies:
        v = verify(b, key)
        ok &= v
        for a in b["alerts"]:
            check_reported(f"response {b['session_id']}", a)
        print(f"  {b['session_id']}: {b['tokens']} tokens, alerts {[a['concept'] for a in b['alerts']]}, "
              f"verifies {v}")
        ok &= check_summaries(b["session_id"], b)
    print(f"  {total} tokens in {dt:.2f} s: {total / dt:.1f} tokens/s aggregate")

    time.sleep(1.5)
    stop.set()
    # The stream carries signed alerts and, tagged kind "summary", each request's signed summaries.
    summaries = [{k: v for k, v in rec.items() if k != "kind"} for _, rec in streamed
                 if rec.get("kind") == "summary"]
    streamed = [(t, rec) for t, rec in streamed if "alert" in rec]
    lat = [t - rec["alert"]["t_token"] for t, rec in streamed]
    sv = all(verify(rec, key) for _, rec in streamed)
    for _, rec in streamed:
        check_reported("stream", rec["alert"])
    print(f"\n/alerts stream: {len(streamed)} signed alerts, all verify: {sv}")
    ssv = all(verify(rec, key) for rec in summaries)
    leaked.extend(("summary stream", s["concept"]) for rec in summaries for s in rec["summaries"]
                  if s["concept"] not in reportable)
    print(f"  and {len(summaries)} signed summary records, all verify: {ssv}")
    ok &= ssv
    if lat:
        print(f"  token -> received on stream: median {statistics.median(lat) * 1000:.1f} ms, "
              f"max {max(lat) * 1000:.1f} ms")
    ok &= sv
    if args.log and args.log.exists():
        events = [e for e in read(args.log) if e["kind"] == "alert"]
        lv = all(verify(e["payload"], key) for e in events)
        for e in events:
            check_reported("alert log", e["payload"]["alert"])
        print(f"alert log {args.log}: {len(events)} alerts, all verify: {lv}")
        records = [e["payload"] for e in read(args.log) if e["kind"] == "summary"]
        rv = all(verify(r, key) for r in records)
        print(f"  and {len(records)} summary records, all verify: {rv}")
        ok &= lv and rv
    print(f"\nonly reported concepts ({args.report.name}: {sorted(reportable)}) crossed the boundary: "
          f"{not leaked}" + (f"; leaked {sorted(set(leaked))}" if leaked else ""))
    ok &= not leaked
    if via_ancestor[1]:
        print(f"covered only via an ancestor (concept itself not scored): {via_ancestor[0]} of "
              f"{via_ancestor[1]} covered concept-tokens ({via_ancestor[0] / via_ancestor[1]:.1%})")
    print("\nSMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
