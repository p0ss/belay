"""The backend's request queue, identity and flags, with generation replaced (no model, no HAT)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from belay.contract import GenerateResponse, key_from_env, verify
from belay.contract.events import read
from belay.contract.service import BackendResult, RawAlert, create_app
from belay.detector.backend import HatBackend
from belay.detector.server import parse_args


class FakeGenerateBackend(HatBackend):
    """HatBackend's queue and identity with the HAT generation replaced."""

    def __init__(self, *args, delay=0.05, **kwargs):
        self.order = []
        self.active = 0
        self.max_active = 0
        self.delay = delay
        self.lock = threading.Lock()
        super().__init__(*args, load_model=False, hash_cache=None, **kwargs)

    def _generate(self, request, emit):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.order.append(request.request_id)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        text = request.messages[-1].content
        alerts = [RawAlert("Law", 0.995, 0, ["Root", "Law"], time.time())] if "law" in text else []
        if emit is not None:  # as the real backend: emitted alerts are not repeated
            for a in alerts:
                emit(a)
            alerts = []
        return BackendResult(completion=f"echo {text}", tokens=3, alerts=alerts,
                             watched=self.watched, resident_peak=self.watched, overhead_ms=0.1)


@pytest.fixture
def fake_model(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"weights")
    return d


def _request(i, text):
    return {"request_id": f"r-{i}", "session_id": f"s-{i}", "agent": f"agent-{i}", "episode": "ep",
            "messages": [{"role": "user", "content": text}], "max_tokens": 8}


def test_four_concurrent_sessions_one_at_a_time(tmp_path, fake_model, fake_pack, profile):
    backend = FakeGenerateBackend(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile)
    assert backend.watched == 1 and backend.profile == "profile"
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    texts = ["constitutional law", "chemistry", "more law", "cooking"]
    with ThreadPoolExecutor(4) as pool:
        responses = list(pool.map(lambda i: client.post("/generate", json=_request(i, texts[i])), range(4)))
    key = key_from_env()
    for i, r in enumerate(responses):
        assert r.status_code == 200
        body = r.json()
        assert verify(body, key)
        GenerateResponse.model_validate(body)
        assert body["session_id"] == f"s-{i}"
        assert bool(body["alerts"]) == ("law" in texts[i])
        assert body["identity"]["model_hash"] == backend.model_hash
        assert body["coverage"]["watch"] == "proxy"
    logged = [e for e in read(tmp_path / "alerts.jsonl") if e["kind"] == "alert"]
    assert sorted(e["payload"]["request_id"] for e in logged) == ["r-0", "r-2"]
    # One request at a time through HAT, every request served.
    assert backend.max_active == 1
    assert sorted(backend.order) == ["r-0", "r-1", "r-2", "r-3"]
    backend.close()


def test_queue_is_fifo(fake_model, fake_pack, profile):
    from belay.contract.models import GenerateRequest

    backend = FakeGenerateBackend(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile,
                                  delay=0.02)
    with ThreadPoolExecutor(4) as pool:
        futures = []
        for i in range(4):
            futures.append(pool.submit(backend.generate, GenerateRequest.model_validate(_request(i, "x"))))
            time.sleep(0.005)  # submitted in order
        [f.result() for f in futures]
    assert backend.order == ["r-0", "r-1", "r-2", "r-3"]
    backend.close()


def test_off_and_tamper_identity(fake_model, fake_pack, profile, tmp_path):
    off = FakeGenerateBackend(watch="off", model=str(fake_model), pack=fake_pack)
    assert off.watched == 0 and off.pack == "none" and off.profile == "none"
    honest = FakeGenerateBackend(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile)
    tampered = FakeGenerateBackend(watch="proxy", tamper="threshold", model=str(fake_model), pack=fake_pack,
                                   profile=profile)
    # Tampering never changes what the detector claims.
    assert (tampered.model_hash, tampered.pack, tampered.profile_hash) == \
        (honest.model_hash, honest.pack, honest.profile_hash)
    assert tampered.threshold > 1.0
    # Neither does the run setting: one certified identity whatever the lab runs.
    others = [FakeGenerateBackend(run=run, model=str(fake_model), pack=fake_pack, profile=profile)
              for run in ("full", "wide")]
    for b in others:
        assert (b.pack, b.profile, b.profile_hash, b.watched) == \
            (honest.pack, honest.profile, honest.profile_hash, 1)
        assert b.report_keys == [("Courts", 2), ("Law", 1)]
    # A report profile other than the run profile: its hash is what is claimed.
    chem = tmp_path / "chem.txt"
    chem.write_text("Chem\n")
    mixed = FakeGenerateBackend(run="proxy", model=str(fake_model), pack=fake_pack, profile=profile, report=chem)
    assert mixed.report_keys == [("Chem", 1)] and mixed.profile == "chem"
    assert mixed.profile_hash != honest.profile_hash
    assert mixed._watch_concepts() == ["Chem", "Law"]  # HAT watches both; only Chem crosses
    with pytest.raises(ValueError):
        FakeGenerateBackend(watch="off", tamper="swap", model=str(fake_model), pack=fake_pack)
    for b in (off, honest, tampered, mixed, *others):
        b.close()


def test_watch_is_an_alias_for_run(fake_model, fake_pack, profile):
    b = FakeGenerateBackend(watch="full", model=str(fake_model), pack=fake_pack, profile=profile)
    assert b.run == b.watch == "full"
    b.close()
    with pytest.raises(ValueError):
        FakeGenerateBackend(run="proxy", watch="full", model=str(fake_model), pack=fake_pack, profile=profile)
    with pytest.raises(ValueError):
        FakeGenerateBackend(run="wide", tamper="nonsense", model=str(fake_model), pack=fake_pack, profile=profile)


def test_backend_error_reaches_caller(fake_model, fake_pack, profile):
    class Broken(FakeGenerateBackend):
        def _generate(self, request, emit):
            raise RuntimeError("boom")

    backend = Broken(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile)
    from belay.contract.models import GenerateRequest

    with pytest.raises(RuntimeError, match="boom"):
        backend.generate(GenerateRequest.model_validate(_request(0, "x")))
    backend.close()


def test_server_flags(tmp_path):
    a = parse_args(["--watch", "wide"])
    assert a.run == "wide"
    a = parse_args(["--host", "0.0.0.0", "--port", "9000", "--watch", "off"])
    assert (a.host, a.port, a.run) == ("0.0.0.0", 9000, "off")
    a = parse_args(["--run", "full", "--report", "profiles/proxy-redlines.txt"])
    assert a.run == "full" and str(a.report).endswith("proxy-redlines.txt")
    assert parse_args([]).run == "proxy"
    with pytest.raises(SystemExit):
        parse_args(["--run", "full", "--watch", "proxy"])
    with pytest.raises(SystemExit):
        parse_args(["--log", str(tmp_path / "x.jsonl"), "--internal-log", str(tmp_path / "x.jsonl")])
    with pytest.raises(SystemExit):  # batching is gone
        parse_args(["--max-batch", "4"])
