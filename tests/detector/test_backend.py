"""The backend's request queue and batching (no model), and the fused probe scorer against HAT's Lens."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from belay.contract import GenerateResponse, key_from_env, verify
from belay.contract.service import BackendResult, RawAlert, create_app
from belay.detector.backend import HatBackend

HIDDEN = 16  # as conftest


class FakeModelBackend(HatBackend):
    """HatBackend's queue and identity with the generation replaced, to test concurrency without a GPU."""

    def __init__(self, *args, delay=0.05, **kwargs):
        self.batches = []
        self.active = 0
        self.max_active = 0
        self.delay = delay
        self.lock = threading.Lock()
        super().__init__(*args, load_model=False, hash_cache=None, **kwargs)

    def _generate_batch(self, requests):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.batches.append([r.request_id for r in requests])
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        out = []
        for r in requests:
            text = r.messages[-1].content
            alerts = [RawAlert("Law", 0.995, 0, ["Root", "Law"], time.time())] if "law" in text else []
            out.append(BackendResult(completion=f"echo {text}", tokens=3, alerts=alerts,
                                     watched=self.watched, resident_peak=self.watched, overhead_ms=0.1))
        return out


@pytest.fixture
def fake_model(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"weights")
    return d


def _request(i, text):
    return {"request_id": f"r-{i}", "session_id": f"s-{i}", "agent": f"agent-{i}", "episode": "ep",
            "messages": [{"role": "user", "content": text}], "max_tokens": 8}


@pytest.mark.parametrize("max_batch", [1, 4])
def test_four_concurrent_sessions_signed(tmp_path, fake_model, fake_pack, profile, max_batch):
    backend = FakeModelBackend(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile,
                               max_batch=max_batch, batch_window_ms=50)
    assert backend.watched == 2 and backend.profile == "profile"
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
    # One generation at a time either way; batching groups waiting requests.
    assert backend.max_active == 1
    sizes = [len(b) for b in backend.batches]
    assert sum(sizes) == 4
    if max_batch == 1:
        assert sizes == [1, 1, 1, 1]
    else:
        assert max(sizes) > 1
    backend.close()


def test_off_and_tamper_identity(fake_model, fake_pack, profile):
    off = FakeModelBackend(watch="off", model=str(fake_model), pack=fake_pack)
    assert off.watched == 0 and off.pack == "none"
    honest = FakeModelBackend(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile)
    tampered = FakeModelBackend(watch="proxy", tamper="threshold", model=str(fake_model), pack=fake_pack,
                                profile=profile)
    # Tampering never changes what the detector claims.
    assert (tampered.model_hash, tampered.pack, tampered.profile_hash) == \
        (honest.model_hash, honest.pack, honest.profile_hash)
    assert tampered.threshold > 1.0
    full = FakeModelBackend(watch="full", model=str(fake_model), pack=fake_pack)
    assert full.watched == 4 and full.profile == "full-pack"
    with pytest.raises(ValueError):
        FakeModelBackend(watch="off", tamper="swap", model=str(fake_model), pack=fake_pack)
    for b in (off, honest, tampered, full):
        b.close()


def test_backend_error_reaches_caller(fake_model, fake_pack, profile):
    class Broken(FakeModelBackend):
        def _generate_batch(self, requests):
            raise RuntimeError("boom")

    backend = Broken(watch="proxy", model=str(fake_model), pack=fake_pack, profile=profile)
    from belay.contract.models import GenerateRequest

    with pytest.raises(RuntimeError, match="boom"):
        backend.generate(GenerateRequest.model_validate(_request(0, "x")))
    backend.close()


def test_fused_probes_match_hat_lens():
    torch = pytest.importorskip("torch")
    lens_types = pytest.importorskip("headspace.monitoring.lens_types")
    from belay.detector.backend import FusedProbes

    torch.manual_seed(1)
    layers = [1, 3, 5]
    spec = {"A": [1, 3], "B": [5], "C": [1, 3, 5]}
    for calibrated in (True, False):
        lenses = []
        for name, ls in spec.items():
            probes = {l: lens_types.SimpleMLP(HIDDEN).eval() for l in ls}
            cal = {l: torch.sort(torch.rand(201))[0] for l in ls} if calibrated else None
            lenses.append(lens_types.Lens(probes, calibration=cal).eval())
        fused = FusedProbes.build(lenses, layers)
        assert fused is not None
        states = {l: torch.randn(3, HIDDEN) for l in layers}
        with torch.inference_mode():
            got = fused(states)
            want = torch.stack([lens(states).float() for lens in lenses])
        assert got.shape == (3, 3)
        assert torch.allclose(got, want, atol=1e-5), (calibrated, got, want)
