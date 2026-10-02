"""What the detector runs versus what it reports, with a tiny stand-in model on the CPU (no GPU)."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from belay.contract import key_from_env, verify
from belay.contract.events import read
from belay.contract.service import create_app
from belay.detector.backend import HatBackend
from belay.detector.server import parse_args

HIDDEN = 16  # as conftest
N_LAYERS = 6  # the fake pack's probes read model layers up to 5


@pytest.fixture
def fake_model_dir(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "model.safetensors").write_bytes(b"weights")
    return d


def _backend(fake_model_dir, pack, profile, **kwargs):
    return HatBackend(model=str(fake_model_dir), pack=pack, profile=profile, load_model=False,
                      hash_cache=None, **kwargs)


def test_run_and_report_are_separate(fake_model_dir, fake_pack, profile, tmp_path):
    # The report profile names Law (and Courts beneath it); full runs all four lenses.
    proxy = _backend(fake_model_dir, fake_pack, profile, run="proxy")
    full = _backend(fake_model_dir, fake_pack, profile, run="full")
    wide = _backend(fake_model_dir, fake_pack, profile, run="wide")
    assert proxy.watch == "proxy" and full.watch == "full" and wide.watch == "wide"
    for b in (proxy, full, wide):
        assert b.report_keys == [("Courts", 2), ("Law", 1)]
        assert b.watched == 2
        assert b.profile == "profile"
    assert len(full.pinned_keys) == 4
    assert proxy.pinned_keys == wide.pinned_keys == [("Courts", 2), ("Law", 1)]
    # One certified identity whatever the lab runs.
    assert {(b.pack, b.profile_hash) for b in (proxy, full, wide)} == {(proxy.pack, proxy.profile_hash)}

    # A report profile other than the run profile: its lenses are added to the run set.
    chem = tmp_path / "chem.txt"
    chem.write_text("Chem\n")
    mixed = _backend(fake_model_dir, fake_pack, profile, run="proxy", report=chem)
    assert mixed.report_keys == [("Chem", 1)]
    assert set(mixed.pinned_keys) == {("Chem", 1), ("Courts", 2), ("Law", 1)}
    assert mixed.profile == "chem" and mixed.profile_hash != proxy.profile_hash
    for b in (proxy, full, wide, mixed):
        b.close()


def test_watch_is_an_alias_for_run(fake_model_dir, fake_pack, profile):
    b = _backend(fake_model_dir, fake_pack, profile, watch="full")
    assert b.run == b.watch == "full"
    b.close()
    with pytest.raises(ValueError):
        _backend(fake_model_dir, fake_pack, profile, run="proxy", watch="full")
    with pytest.raises(ValueError):
        _backend(fake_model_dir, fake_pack, profile, run="wide", tamper="nonsense")


def test_server_flags(tmp_path):
    a = parse_args(["--watch", "wide"])
    assert a.run == "wide"
    a = parse_args(["--run", "full", "--report", "profiles/proxy-redlines.txt"])
    assert a.run == "full" and str(a.report).endswith("proxy-redlines.txt")
    assert parse_args([]).run == "proxy"
    with pytest.raises(SystemExit):
        parse_args(["--run", "full", "--watch", "proxy"])
    with pytest.raises(SystemExit):
        parse_args(["--log", str(tmp_path / "x.jsonl"), "--internal-log", str(tmp_path / "x.jsonl")])


# --------------------------------------------------------------- a stand-in model


class _Tok:
    eos_token_id = 0
    pad_token_id = 0

    def apply_chat_template(self, conversations, **kwargs):
        torch = pytest.importorskip("torch")
        return {"input_ids": torch.ones(len(conversations), 3, dtype=torch.long),
                "attention_mask": torch.ones(len(conversations), 3, dtype=torch.long)}

    def decode(self, ids, skip_special_tokens=True):
        return " ".join(map(str, ids))


def _model():
    torch = pytest.importorskip("torch")

    class Model(torch.nn.Module):
        """A forward pass that returns N_LAYERS + 1 hidden states; generate calls it once per token."""

        def __init__(self):
            super().__init__()
            self.generation_config = SimpleNamespace(eos_token_id=0)
            self.dummy = torch.nn.Parameter(torch.zeros(1))

        @property
        def device(self):
            return torch.device("cpu")

        def forward(self, input_ids=None, output_hidden_states=False):
            b = input_ids.shape[0]
            g = torch.Generator().manual_seed(int(input_ids.sum()))
            hidden = tuple(torch.randn(b, 1, HIDDEN, generator=g) for _ in range(N_LAYERS + 1))
            return SimpleNamespace(hidden_states=hidden if output_hidden_states else None)

        def generate(self, input_ids=None, max_new_tokens=1, output_hidden_states=False, **kwargs):
            seq = input_ids
            for i in range(max_new_tokens):
                self(input_ids=seq, output_hidden_states=output_hidden_states)
                seq = torch.cat([seq, torch.full((seq.shape[0], 1), 5 + i)], dim=1)
            return SimpleNamespace(sequences=seq)

    return Model()


class _FakeDynamic:
    """Stands in for the First Light pack under HAT's dynamic loading."""
    name = "wide-pack"
    threshold = 0.5
    total = 7947
    warm = 100

    def __init__(self):
        self.resident = 5
        self.step_peak = 5

    def read(self, hidden):
        assert hidden.shape == (1, HIDDEN)
        self.step_peak = 41
        self.resident = 15
        return [("Deception", 0.9, 4), ("Tent", 0.2, 3)]

    def path(self, concept, layer):
        return ["MindsAndAgents", concept]


def _loaded(backend, wide=False):
    """Load the fake pack's lenses into `backend` as _load does, with the stand-in model."""
    from belay.detector.backend import Lenses
    from belay.detector.pack import Hierarchy

    backend.tokenizer, backend.model = _Tok(), _model()
    backend.lenses = Lenses(backend._pack_dir, backend.pinned_keys, Hierarchy(backend._pack_dir), "cpu")
    if wide:
        backend.dynamic = _FakeDynamic()
        backend._wide_layer = N_LAYERS - 1
    return backend


def _req(i, text="anything"):
    return {"request_id": f"r-{i}", "session_id": f"s-{i}", "agent": f"agent-{i}", "episode": "ep",
            "messages": [{"role": "user", "content": text}], "max_tokens": 4}


@pytest.mark.parametrize("run", ["full", "wide"])
def test_only_reported_concepts_cross_the_boundary(tmp_path, fake_model_dir, torch_pack, profile, run):
    internal = tmp_path / "internal.jsonl"
    # Threshold 0: every lens crosses on the first token, reported or not.
    backend = _loaded(_backend(fake_model_dir, torch_pack, profile, run=run, threshold=0.0,
                               internal_log=internal), wide=run == "wide")
    backend.threshold = 0.0
    boundary = tmp_path / "alerts.jsonl"
    client = TestClient(create_app(backend, boundary))
    key = key_from_env()
    reported = {"Law", "Courts"}

    body = client.post("/generate", json=_req(0)).json()
    assert verify(body, key)
    assert {a["concept"] for a in body["alerts"]} == reported
    cov = body["coverage"]
    assert cov["watched"] == 2 and cov["profile"] == "profile" and cov["watch"] == run
    assert cov["resident_peak"] == (2 + 41 if run == "wide" else 4)
    # The boundary log holds only reported concepts, and everything else the
    # lab scored is in the internal log.
    logged = [e["payload"]["alert"]["concept"] for e in read(boundary) if e["kind"] == "alert"]
    assert set(logged) == reported
    whole = json.dumps(body) + boundary.read_text()
    # Non-reported concepts appear nowhere (Root only as an ancestor in a reported path).
    for hidden in ("Chem", "Deception", "MindsAndAgents"):
        assert f'"{hidden}"' not in whole
    rows = [json.loads(line) for line in internal.read_text().splitlines()]
    crossed = {r["concept"] for r in rows if r["kind"] == "crossing"}
    # full pins the whole pack; wide pins only the reported lenses and loads the wide pack.
    want = {"Deception"} if run == "wide" else {"Root", "Chem"}
    assert crossed == want
    summary = [r for r in rows if r["kind"] == "request"][0]
    assert summary["run"] == run and summary["resident_peak"] == cov["resident_peak"]
    backend.close()


def test_tamper_off_drops_reported_but_claims_them(tmp_path, fake_model_dir, torch_pack, profile):
    internal = tmp_path / "internal.jsonl"
    backend = _loaded(_backend(fake_model_dir, torch_pack, profile, run="full", tamper="off",
                               internal_log=internal))
    backend.threshold = 0.0
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    assert body["alerts"] == []
    assert body["coverage"]["watched"] == 2  # the lie
    crossed = {json.loads(l)["concept"] for l in internal.read_text().splitlines() if '"crossing"' in l}
    assert crossed == {"Root", "Chem"}
    backend.close()


def test_threshold_tamper_applies_to_reported(tmp_path, fake_model_dir, torch_pack, profile):
    backend = _loaded(_backend(fake_model_dir, torch_pack, profile, run="proxy", tamper="threshold"))
    assert backend.threshold > 1.0
    client = TestClient(create_app(backend, tmp_path / "alerts.jsonl"))
    body = client.post("/generate", json=_req(0)).json()
    assert body["alerts"] == [] and body["coverage"]["watched"] == 2
    backend.close()
