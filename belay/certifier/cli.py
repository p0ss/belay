"""
belay-certifier: certify a detector from outside the box, verify it in
operation, and audit its alert log. See belay/certifier/README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from belay.contract import key_from_env

from .certify import run_certification
from .checks import Baseline, audit_log
from .suite import load_canaries, load_suite
from .traffic import make_client
from .verify import load_certificate, verify_direct, verify_swarm

OUT = Path("runs/certifier")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write(results: dict, out: Optional[str], name: str) -> Path:
    path = Path(out) if out else OUT / f"{name}-{_stamp()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, default=str))
    return path


def _certificate(arg: Optional[str]):
    path = Path(arg) if arg else OUT / "certificate-latest.json"
    if not path.exists():
        if arg:
            raise SystemExit(f"no certificate at {path}")
        print("no certificate given: the first response's identity is the baseline", file=sys.stderr)
        return None
    return load_certificate(path)


def cmd_certify(a) -> int:
    suite = load_suite(Path(a.suite), need_cases=True)
    with make_client(a.url) as client:
        r = run_certification(client, suite, seed=a.seed, concurrency=a.concurrency, episode=a.episode,
                              alerts_log=Path(a.alerts_log) if a.alerts_log else None,
                              install=not a.no_install, out_dir=OUT, url=a.url)
    path = _write(r, a.out, "certify")
    s = r["concept_score"]
    roles = ", ".join(f"{role} {v['passed']}/{v['total']}" for role, v in s["by_role"].items())
    print(f"certification {'PASSED' if r['passed'] else 'FAILED'} on {r['responses']}/{r['cases']} cases: "
          f"{s['passed']}/{s['total']} concepts met their criteria ({s['share']:.0%}; {roles})")
    for c, v in r["concepts"].items():
        rec = "n/a" if v["recall"] is None else f"{v['recall']:.3f}"
        fpr = "n/a" if v["fpr"] is None else f"{v['fpr']:.3f}"
        print(f"  {c:20s} {v['role']:10s} recall {rec} ({v['hits']}/{v['positives']}, min {v['recall_min']})  "
              f"FPR {fpr} ({v['false_alarms']}/{v['negatives']}, max {v['fpr_max']})  "
              f"{'ok' if v['passed'] else 'FAIL ' + '; '.join(v['reasons'])}")
    for k, v in r["checks"].items():
        if not v["passed"]:
            print(f"  check {k}: {v['failures']} failures")
    if r["certificate"]:
        print(f"certificate {r['certificate']['certificate_id']} until {r['certificate']['certified_until']}"
              f" -> {r.get('certificate_path')}; installed: {r['installed']}")
    print(f"results -> {path}")
    return 0 if r["passed"] else 1


def _print_verify(r: dict, path: Path) -> None:
    e = r["exposure"]
    cr, fa = r["canary_recall"], r["canary_false_alarms"]
    print(f"verification ({r['mode']}): {r['requests']} requests, {r['canaries']} canaries, "
          f"{r['decoys']} decoys in {r['seconds']}s")
    print(f"  canary recall {cr['hits']}/{cr['positives']}; false alarms {fa['false_alarms']}/{fa['negatives']}")
    if e:
        print(f"EXPOSED by {e['check']} after {e['requests']} requests and {e['seconds']}s: {e['detail']}")
        for k, v in r["exposures_by_check"].items():
            if k != e["check"]:
                print(f"  also {k} after {v['requests']} requests: {v['detail']}")
    elif not r["enough_canaries"]:
        print("INCONCLUSIVE: too few canaries scored")
    else:
        print("PASSED: nothing exposed")
    print(f"results -> {path}")


def _baseline_from(path: str) -> Baseline:
    """The identity measured in a certification run, certified or not.

    A detector that failed certification has no certificate, but its behaviour
    in operation can still be checked against what was measured.
    """
    ident = json.loads(Path(path).read_text())["identity"]
    return Baseline(ident["model_hash"], ident["pack"], ident["profile_hash"])


def cmd_verify(a) -> int:
    baseline = _baseline_from(a.baseline_from) if a.baseline_from else None
    cert = None if baseline else _certificate(a.certificate)
    if a.episode_log:
        suite = load_suite(Path(a.suite), need_cases=False, need_canaries=True)
        r = verify_swarm(suite.canaries, suite.criteria, episode_log=Path(a.episode_log),
                         alerts_log=Path(a.alerts_log) if a.alerts_log else None, certificate=cert,
                         baseline=baseline)
    else:
        if not a.url:
            raise SystemExit("verify needs --url (direct) or --episode-log (swarm)")
        suite = load_suite(Path(a.suite), need_cases=False, need_canaries=True)
        decoys = load_canaries(Path(a.decoys), decoy=True) if a.decoys else None
        with make_client(a.url) as client:
            r = verify_direct(client, suite.canaries, suite.criteria, certificate=cert, baseline=baseline, decoys=decoys,
                              decoy_ratio=a.decoy_ratio, rounds=a.rounds, max_gap=a.max_gap, seed=a.seed,
                              stop_on_expose=not a.no_stop, max_requests=a.max_requests,
                              alerts_log=Path(a.alerts_log) if a.alerts_log else None, episode=a.episode)
        r["url"] = a.url
    path = _write(r, a.out, "verify")
    _print_verify(r, path)
    return 0 if r["passed"] else (1 if r["exposed"] else 2)


def cmd_audit(a) -> int:
    audit = audit_log(Path(a.log), key_from_env())
    d = audit.as_dict()
    print(f"{d['path']}: {d['alert_records']} alert records, {d['verified']} verified, "
          f"{len(d['altered'])} altered, {len(d['duplicates'])} duplicated")
    for x in d["altered"]:
        print(f"  ALTERED line {x['line']}: {x['reason']}" + (f" ({x['request_id']})" if x.get("request_id") else ""))
    for x in d["duplicates"]:
        print(f"  duplicate line {x['line']} repeats line {x['first_line']}")
    if a.out:
        _write(d, a.out, "audit")
    return 0 if audit.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="belay-certifier", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("certify", help="certify a detector against a suite")
    c.add_argument("--url", required=True)
    c.add_argument("--suite", default="belay/certifier/suites")
    c.add_argument("--alerts-log", help="the detector's alert log, to audit alongside")
    c.add_argument("--concurrency", type=int, default=4)
    c.add_argument("--episode", help="episode id to put on requests (default: one named like a swarm episode)")
    c.add_argument("--seed", type=int)
    c.add_argument("--no-install", action="store_true", help="issue the certificate but do not POST it")
    c.add_argument("--out", help="results JSON path")
    c.set_defaults(fn=cmd_certify)

    v = sub.add_parser("verify", help="verify a certified detector in operation with canaries")
    v.add_argument("--suite", default="belay/certifier/suites")
    v.add_argument("--certificate", help="default runs/certifier/certificate-latest.json")
    v.add_argument("--baseline-from", help="a certify results JSON: check against the identity it measured, "
                                           "without a certificate (for a detector that failed certification)")
    v.add_argument("--alerts-log", help="the detector's alert log")
    v.add_argument("--out", help="results JSON path")
    g = v.add_argument_group("direct: the certifier sends canaries between decoys")
    g.add_argument("--url")
    g.add_argument("--direct", action="store_true", help="(default when --episode-log is not given)")
    g.add_argument("--decoys", help="JSONL of decoy tasks, canary format (default: built-in distant fields)")
    g.add_argument("--decoy-ratio", type=float, default=2.0, help="mean decoys per canary")
    g.add_argument("--rounds", type=int, default=1, help="passes over the canaries")
    g.add_argument("--max-gap", type=float, default=1.0, help="max random gap between requests, seconds")
    g.add_argument("--max-requests", type=int)
    g.add_argument("--no-stop", action="store_true", help="keep going after the first exposure")
    g.add_argument("--episode")
    g.add_argument("--seed", type=int)
    s = v.add_argument_group("swarm: canaries the swarm mixed into live traffic")
    s.add_argument("--episode-log", help="the swarm's episode log")
    v.set_defaults(fn=cmd_verify)

    l = sub.add_parser("audit-log", help="verify every signed alert record in a detector log")
    l.add_argument("--log", required=True)
    l.add_argument("--out")
    l.set_defaults(fn=cmd_audit)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
