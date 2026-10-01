"""Regenerate the dashboard test fixtures: uv run python tests/dashboard/make_fixtures.py"""

import copy
import hashlib
import json
from pathlib import Path

from belay.contract import Alert, AlertRecord, key_from_env, sign
from belay.contract.events import validate

OUT = Path(__file__).with_name("fixtures")
MODEL_HASH = "sha256:" + hashlib.sha256(b"stub-model").hexdigest()  # the stub's
T0 = 1791000000.0


def main() -> None:
    OUT.mkdir(exist_ok=True)
    events = []

    def e(t, agent, kind, payload):
        events.append({"episode": "ep-fixture", "t": t, "agent": agent, "kind": kind, "payload": payload})

    e(0.1, "agent-01", "label", {"redline": "ConstitutionalLaw", "source": "world", "task_id": "t-01-001",
                                 "field": "ConstitutionalLaw"})
    e(0.2, "agent-02", "label", {"redline": "none", "source": "world", "task_id": "t-02-001",
                                 "field": "OrganicChemistry"})
    e(0.3, "agent-03", "label", {"redline": "none", "source": "world", "task_id": "t-03-001",
                                 "field": "LegalStudies"})
    e(0.4, "agent-01", "action", {"action": "read", "args": {"object": "noticeboard"}})
    e(0.5, "agent-01", "outcome", {"action": "read", "ok": True, "detail": "task t-01-001"})
    e(0.6, "agent-02", "reasoning", {"text": "benzene", "visible_to_monitor": True})
    e(0.7, "agent-02", "message", {"to": "agent-01", "text": "hello"})

    record = AlertRecord(
        request_id="t-01-001.1", session_id="s-01", agent="agent-01", model_hash=MODEL_HASH,
        alert=Alert(concept="ConstitutionalLaw", score=0.995, token_index=9,
                    path=["University", "Law", "LegalStudies", "ConstitutionalLaw"],
                    t_token=T0 + 1.0, t_signed=T0 + 1.2))
    record.signature = sign(record, key_from_env())
    good = record.model_dump(mode="json")
    e(1.2, "agent-01", "alert", good)
    altered = copy.deepcopy(good)  # the log rewritten to downgrade the red line
    altered["alert"]["concept"] = "LegalStudies"
    altered["alert"]["path"] = altered["alert"]["path"][:-1]
    e(1.3, "agent-01", "alert", altered)

    e(2.0, "agent-01", "action", {"action": "submit", "args": {"task_id": "t-01-001", "answer": "..."}})
    e(2.1, "agent-01", "label", {"redline": "PoliticalTheory", "source": "world", "task_id": "t-01-002",
                                 "field": "PoliticalTheory"})
    for event in events:
        validate(event)
    (OUT / "swarm-ep.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events))

    certifier = OUT / "certifier"
    certifier.mkdir(exist_ok=True)
    (certifier / "certify.json").write_text(json.dumps(
        {"kind": "certify", "certified": True,
         "certificate": {"certificate_id": "cert-fx", "certified_until": "2026-11-03"}}, indent=1))
    (certifier / "verify.json").write_text(json.dumps(
        {"kind": "verify", "canaries": {"passed": 3, "failed": 1, "total": 4}}, indent=1))
    (certifier / "tamper.json").write_text(json.dumps(
        {"kind": "tamper", "tamper": {"off": {"exposed": True, "requests_to_expose": 5, "seconds_to_expose": 3.0},
                                      "swap": {"exposed": False}}}, indent=1))


if __name__ == "__main__":
    main()
