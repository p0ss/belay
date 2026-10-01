import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from belay.contract.service import create_app
from belay.contract.stub import StubBackend
from belay.certifier.cli import main

from .conftest import SUITE


def _request(i, text):
    return {"request_id": f"{i:012x}.0", "session_id": "s-1", "agent": "agent-1", "episode": "ep",
            "messages": [{"role": "user", "content": text}]}


def _fill(client):
    for i, text in enumerate(["judicial review under the constitution", "hobbes and the social contract",
                              "a statute in court", "benzene rings"]):
        client.post("/generate", json=_request(i, text))


def test_audit_log_clean_then_catches_edited_line(stub, capsys):
    client, log = stub()
    _fill(client)
    assert main(["audit-log", "--log", str(log)]) == 0
    lines = log.read_text().splitlines()
    assert len(lines) == 3
    ev = json.loads(lines[1])
    ev["payload"]["alert"]["concept"] = "Astronomy"  # rewrite one alert after the fact
    lines[1] = json.dumps(ev)
    lines.append("{not json")
    log.write_text("\n".join(lines) + "\n")
    capsys.readouterr()
    assert main(["audit-log", "--log", str(log)]) == 1
    out = capsys.readouterr().out
    assert "ALTERED line 2" in out and "ALTERED line " + str(len(lines)) in out


def test_audit_log_flags_duplicated_line(stub, capsys):
    client, log = stub()
    _fill(client)
    lines = log.read_text().splitlines()
    log.write_text("\n".join(lines + [lines[0]]) + "\n")
    assert main(["audit-log", "--log", str(log)]) == 0  # still authentic, but reported
    assert "duplicate line" in capsys.readouterr().out


class Server:
    """A real stub detector over HTTP, as `uv run belay-stub --tamper ...` would run it."""

    def __init__(self, tamper, log, seed=0):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        app = create_app(StubBackend(tamper, seed=seed, latency_ms=2), log)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("stub did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


def test_cli_certify_then_verify_over_http(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "honest.jsonl"
    with Server("none", log) as srv:
        rc = main(["certify", "--url", srv.url, "--suite", str(SUITE), "--alerts-log", str(log), "--seed", "1",
                   "--out", "cert.json"])
        assert rc == 0, capsys.readouterr().out
        cert = json.loads(Path("runs/certifier/certificate-latest.json").read_text())
        assert json.loads(Path("cert.json").read_text())["installed"]["status"] == 200
        assert main(["verify", "--direct", "--url", srv.url, "--suite", str(SUITE), "--max-gap", "0",
                     "--alerts-log", str(log), "--out", "v.json"]) == 0
        assert json.loads(Path("v.json").read_text())["certificate_id"] == cert["certificate_id"]

    for tamper in ("off", "threshold", "swap"):
        tlog = tmp_path / f"{tamper}.jsonl"
        with Server(tamper, tlog, seed=11) as srv:
            # The tampered detector carries the same certificate.
            import httpx
            assert httpx.post(srv.url + "/certificate", json=cert).status_code == 200
            assert main(["certify", "--url", srv.url, "--suite", str(SUITE), "--no-install",
                         "--out", f"c-{tamper}.json"]) == 1
            rc = main(["verify", "--url", srv.url, "--suite", str(SUITE), "--max-gap", "0.01", "--rounds", "10",
                       "--alerts-log", str(tlog), "--out", f"v-{tamper}.json"])
            r = json.loads(Path(f"v-{tamper}.json").read_text())
            assert rc == 1 and r["exposed"], r
            assert r["exposure"]["seconds"] > 0
