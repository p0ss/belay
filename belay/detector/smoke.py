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
Prints per-token latency and token-to-signed-alert latency.
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
    print(f"  {total} tokens in {dt:.2f} s: {total / dt:.1f} tokens/s aggregate")

    time.sleep(1.5)
    stop.set()
    lat = [t - rec["alert"]["t_token"] for t, rec in streamed]
    sv = all(verify(rec, key) for _, rec in streamed)
    for _, rec in streamed:
        check_reported("stream", rec["alert"])
    print(f"\n/alerts stream: {len(streamed)} signed alerts, all verify: {sv}")
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
        ok &= lv
    print(f"\nonly reported concepts ({args.report.name}: {sorted(reportable)}) crossed the boundary: "
          f"{not leaked}" + (f"; leaked {sorted(set(leaked))}" if leaked else ""))
    ok &= not leaked
    print("\nSMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
