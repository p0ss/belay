"""
The thin boundary over HAT's real Monitor, with a tiny stand-in model on the CPU.

HAT's `Monitor.from_pretrained` is replaced by one that builds the same Monitor
(HAT's DynamicLensManager with from_pretrained's settings) around the stand-in
model; everything after that is HAT as shipped: its generate loop, hierarchical
loading, scoring, WatchProfile and Steps.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

torch = pytest.importorskip("torch")
runtime = pytest.importorskip("headspace.runtime")

from belay.contract import key_from_env, verify  # noqa: E402
from belay.contract.events import read  # noqa: E402
from belay.contract.service import create_app  # noqa: E402
from belay.detector.backend import HAT_MANAGER_DEFAULTS, HatBackend  # noqa: E402

HIDDEN = 16  # as conftest
N_LAYERS = 6  # the fake pack's probes read model layers up to 5
REPORTED = {"Law", "Courts"}


class Tok:
    eos_token_id = 0
    pad_token_id = 0

    def apply_chat_template(self, messages, **kwargs):
        return {"input_ids": torch.ones(1, 3, dtype=torch.long)}

    def decode(self, ids, skip_special_tokens=False):
        ids = ids.tolist() if hasattr(ids, "tolist") else ids
        ids = [ids] if isinstance(ids, int) else ids
        return " ".join(f"t{i}" for i in ids)


class Model(torch.nn.Module):
    """Emits tokens 5, 6, 7, ... (0, the stop token, at `stop_at`), with random hidden states."""

    def __init__(self, stop_at=None):
        super().__init__()
        self.generation_config = SimpleNamespace(eos_token_id=0)
        self.config = SimpleNamespace(get_text_config=lambda: SimpleNamespace(hidden_size=HIDDEN,
                                                                              num_hidden_layers=N_LAYERS))
        self.dummy = torch.nn.Parameter(torch.zeros(1))
        self.calls = 0
        self.stop_at = stop_at

    @property
    def device(self):
        return torch.device("cpu")

    def _next(self, step):
        return 0 if step == self.stop_at else 5 + step

    def forward(self, input_ids, past_key_values=None, use_cache=True, output_hidden_states=False):
        self.calls += 1
        step = 0 if past_key_values is None else past_key_values
        g = torch.Generator().manual_seed(step)
        hidden = tuple(torch.randn(1, input_ids.shape[1], HIDDEN, generator=g) for _ in range(N_LAYERS + 1))
        logits = torch.zeros(1, input_ids.shape[1], 32)
        logits[0, -1, self._next(step)] = 1.0
        return SimpleNamespace(logits=logits, past_key_values=step + 1,
                               hidden_states=hidden if output_hidden_states else None)

    def generate(self, input_ids, max_new_tokens=1, eos_token_id=None, **kwargs):
        new = []
        for step in range(max_new_tokens):
            new.append(self._next(step))
            if new[-1] in (eos_token_id or []):
                break
        return torch.cat([input_ids, torch.tensor([new])], dim=1)


@pytest.fixture
def stand_in(monkeypatch):
    """Monitor.from_pretrained around a stand-in model; returns the model and the calls made."""
    from headspace.monitoring.lens_manager import DynamicLensManager

    model = Model()
    calls = []

    def from_pretrained(model_id, pack_dir, hierarchy_dir=None, device="cuda", watch=None, **kwargs):
        calls.append({"pack_dir": pack_dir, "hierarchy_dir": hierarchy_dir, "watch": watch})
        extra = {"layers_data_dir": hierarchy_dir} if hierarchy_dir is not None else {}
        manager = DynamicLensManager(lenses_dir=pack_dir, device="cpu", **HAT_MANAGER_DEFAULTS, **extra)
        return runtime.Monitor(model, Tok(), manager, watch=watch)

    monkeypatch.setattr(runtime.Monitor, "from_pretrained", staticmethod(from_pretrained))
    return SimpleNamespace(model=model, calls=calls)


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"weights")
    return d


def _backend(model_dir, pack, profile, stand_in=None, **kwargs):
    b = HatBackend(model=str(model_dir), pack=pack, profile=profile, load_model=False, hash_cache=None,
                   device="cpu", **kwargs)
    if b.run == "off" or (b.run == "proxy" and b.tamper == "off"):
        b.model, b.tokenizer = Model(), Tok()
    else:
        b._load()
    return b


def _req(i, tokens=4):
    return {"request_id": f"r-{i}", "session_id": f"s-{i}", "agent": f"agent-{i}", "episode": "ep",
            "messages": [{"role": "user", "content": "anything"}], "max_tokens": tokens}


def _internal(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("run", ["proxy", "full", "wide"])
def test_only_reported_concepts_cross(tmp_path, model_dir, torch_pack, wide_pack, profile, stand_in, run):
    internal = tmp_path / "internal.jsonl"
    # Threshold 0: every detection HAT returns alerts, reported or not.
    backend = _backend(model_dir, torch_pack, profile, stand_in, run=run, threshold=0.0, internal_log=internal,
                       wide_pack=wide_pack)
    boundary = tmp_path / "alerts.jsonl"
    client = TestClient(create_app(backend, boundary))
    key = key_from_env()

    body = client.post("/generate", json=_req(0)).json()
    assert verify(body, key)
    assert body["tokens"] == 4 and body["completion"] == "t5 t6 t7 t8"
    alerts = body["alerts"]
    # HAT alerts on every lens it scored (not only its top-k detections), so
    # both reported concepts cross; each alerts once, at its first crossing.
    assert [a["concept"] for a in alerts] == ["Law", "Courts"]
    courts = alerts[1]
    assert courts["path"] == ["Root", "Law", "Courts"] and courts["token_index"] == 0
    cov = body["coverage"]
    # Law and Courts were both scored by HAT on this request.
    assert cov["watched"] == 2 and cov["profile"] == "profile" and cov["watch"] == run
    assert cov["pack"] == "fake-pack"
    assert cov["resident_peak"] >= 1 and body["overhead_ms"] > 0
    logged = [e["payload"]["alert"]["concept"] for e in read(boundary) if e["kind"] == "alert"]
    assert logged == ["Law", "Courts"]
    whole = json.dumps(body) + boundary.read_text()
    for hidden in ("Chem", "Deception", "Tent", "Mind"):
        assert f'"{hidden}"' not in whole
    rows = _internal(internal)
    crossed = {r["concept"] for r in rows if r["kind"] == "crossing"}
    if run == "proxy":
        assert crossed == set()  # HAT watches only Law's branch
    elif run == "full":
        # HAT alerts on every lens scored, so the unreported Root crosses too, internally.
        assert crossed == {"Chem", "Root"}
        assert "Root" not in [a["concept"] for a in alerts]
    else:  # the wide pack's detections, all lab-internal
        assert crossed and crossed <= {"Deception", "Tent"}
        assert {r["pack"] for r in rows if r["kind"] == "crossing"} == {"wide-pack"}
    summary = [r for r in rows if r["kind"] == "request"][0]
    assert summary["run"] == run and summary["resident_peak"] == cov["resident_peak"]
    backend.close()


def test_wide_runs_two_monitors_on_one_forward_pass(tmp_path, model_dir, torch_pack, wide_pack, profile, stand_in):
    backend = _backend(model_dir, torch_pack, profile, stand_in, run="wide", threshold=0.0, internal_log=None,
                       wide_pack=wide_pack)
    assert stand_in.calls[0]["pack_dir"] == wide_pack
    assert backend.monitor.lenses.lenses_dir.name == "wide-pack"
    assert backend.reported.lenses.lenses_dir.name == "fake-pack"
    assert backend.reported.model is backend.monitor.model
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0, tokens=3)).json()
    assert body["tokens"] == 3
    assert stand_in.model.calls == 3  # one forward pass per token, not two
    # The capture hook is removed after the request.
    assert not stand_in.model._forward_hooks
    # resident_peak counts both packs' resident lenses.
    assert body["coverage"]["resident_peak"] > len(backend.monitor.lenses.cache.loaded_lenses)
    backend.close()


@pytest.mark.parametrize("run", ["proxy", "wide"])
def test_alerts_are_emitted_per_step(model_dir, torch_pack, wide_pack, profile, stand_in, run):
    from belay.contract.models import GenerateRequest

    backend = _backend(model_dir, torch_pack, profile, stand_in, run=run, threshold=0.0, internal_log=None,
                       wide_pack=wide_pack)
    seen = []
    result = backend.generate(GenerateRequest.model_validate(_req(0, tokens=4)),
                              emit=lambda a: seen.append((a, stand_in.model.calls)))
    assert [a.concept for a, _ in seen] == ["Law", "Courts"]
    alert, calls = seen[0]
    # Emitted after the forward pass of its token and before the next one.
    assert calls == alert.token_index + 1
    assert result.alerts == []  # emitted alerts are not repeated
    backend.close()


def test_without_emit_alerts_are_returned(model_dir, torch_pack, profile, stand_in):
    from belay.contract.models import GenerateRequest

    backend = _backend(model_dir, torch_pack, profile, stand_in, run="full", threshold=0.0, internal_log=None)
    result = backend.generate(GenerateRequest.model_validate(_req(0)))
    assert [a.concept for a in result.alerts] == ["Law", "Courts"]
    backend.close()


def test_stop_token_ends_generation(model_dir, torch_pack, profile, stand_in):
    from belay.contract.models import GenerateRequest

    stand_in.model.stop_at = 2
    backend = _backend(model_dir, torch_pack, profile, stand_in, run="proxy", internal_log=None)
    result = backend.generate(GenerateRequest.model_validate(_req(0, tokens=8)))
    assert result.tokens == 3 and result.completion == "t5 t6 t0"
    backend.close()


def test_off_is_plain_generation(tmp_path, model_dir, torch_pack, profile):
    internal = tmp_path / "internal.jsonl"
    backend = _backend(model_dir, torch_pack, profile, run="off", internal_log=internal)
    assert backend.monitor is None
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    assert body["completion"] == "t5 t6 t7 t8" and body["tokens"] == 4
    assert body["alerts"] == [] and body["overhead_ms"] == 0
    assert body["coverage"] == {"watched": 0, "resident_peak": 0, "pack": "none", "profile": "none",
                                "watch": "off"}
    assert not internal.exists()
    backend.close()


def test_tamper_off_proxy_runs_nothing_but_claims_coverage(tmp_path, model_dir, torch_pack, profile):
    backend = _backend(model_dir, torch_pack, profile, run="proxy", tamper="off", threshold=0.0,
                       internal_log=None)
    assert backend.monitor is None
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    assert body["alerts"] == []
    assert body["coverage"]["watched"] == 2  # the lie
    assert body["overhead_ms"] > 0  # and a plausible cost
    assert body["coverage"]["watch"] == "proxy" and body["coverage"]["pack"] == "fake-pack"
    backend.close()


@pytest.mark.parametrize("run", ["full", "wide"])
def test_tamper_off_drops_reported_but_claims_them(tmp_path, model_dir, torch_pack, wide_pack, profile, stand_in,
                                                   run):
    internal = tmp_path / "internal.jsonl"
    backend = _backend(model_dir, torch_pack, profile, stand_in, run=run, tamper="off", threshold=0.0,
                       internal_log=internal, wide_pack=wide_pack)
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    assert body["alerts"] == []
    assert body["coverage"]["watched"] == 2  # the lie
    crossed = {r["concept"] for r in _internal(internal) if r["kind"] == "crossing"}
    assert "Courts" not in crossed and crossed  # the rest still runs; reported readings are thrown away
    if run == "wide":
        assert backend.reported is None  # the university pack does not run at all
    backend.close()


@pytest.mark.parametrize("run", ["proxy", "wide"])
def test_threshold_tamper_silences_reported(tmp_path, model_dir, torch_pack, wide_pack, profile, stand_in, run):
    backend = _backend(model_dir, torch_pack, profile, stand_in, run=run, tamper="threshold", internal_log=None,
                       wide_pack=wide_pack)
    assert backend.threshold > 1.0 and backend.reported.watch.threshold > 1.0
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    # The lenses still run (honest coverage); nothing can cross the threshold.
    assert body["alerts"] == [] and body["coverage"]["watched"] == 2
    backend.close()


def test_swap_loads_reported_lenses_from_untrained_pack(tmp_path, model_dir, torch_pack, profile, stand_in):
    swap_dir = tmp_path / "untrained"
    honest = _backend(model_dir, torch_pack, profile, stand_in, run="proxy", internal_log=None)
    swapped = _backend(model_dir, torch_pack, profile, stand_in, run="proxy", tamper="swap", internal_log=None,
                       swap_dir=swap_dir)
    assert stand_in.calls[-1]["pack_dir"] == swap_dir / torch_pack.name
    assert (swapped.pack, swapped.profile_hash) == (honest.pack, honest.profile_hash)
    assert swapped.monitor.lenses.lenses_dir == swap_dir / torch_pack.name
    # The reported lenses are untrained copies; the rest are the original files.
    assert not (swap_dir / torch_pack.name / "layer1" / "Law@L2.pt").is_symlink()
    assert (swap_dir / torch_pack.name / "layer1" / "Chem@L1.pt").is_symlink()
    client = TestClient(create_app(swapped, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    assert body["coverage"]["watched"] == 2  # HAT scores the swapped lenses as if they were real
    honest.close()
    swapped.close()
