"""Pack reading, the swap pack, and log tampering. No GPU or model needed."""

import json
from pathlib import Path

import pytest

from belay.contract import AlertRecord, key_from_env, sign, verify
from belay.contract.events import EventLog, read
from belay.contract.models import Alert
from belay.detector.pack import Hierarchy, full_profile_text, lens_files, read_profile, watched_keys
from belay.detector.tamper_log import tamper

REAL_PACK = Path("/var/home/poss/Documents/Code/HatCatDev/lens_packs/gemma-4-e4b-it_university-v3-contrasts-bands")
PROXY = Path(__file__).resolve().parents[2] / "profiles" / "proxy-redlines.txt"


def test_watch_profile_covers_branch(fake_pack, profile):
    lensed = lens_files(fake_pack)
    assert set(lensed) == {("Root", 0), ("Law", 1), ("Chem", 1), ("Courts", 2)}
    assert len(lensed[("Law", 1)]) == 2
    h = Hierarchy(fake_pack)
    assert h.path(("Courts", 2)) == ["Root", "Law", "Courts"]
    assert watched_keys(read_profile(profile), lensed, h) == [("Courts", 2), ("Law", 1)]
    assert full_profile_text(h, lensed).splitlines()[1:] == ["Root"]


@pytest.mark.skipif(not REAL_PACK.exists(), reason="university pack not present")
def test_proxy_profile_on_real_pack():
    keys = watched_keys(read_profile(PROXY), lens_files(REAL_PACK), Hierarchy(REAL_PACK))
    assert {k[0] for k in keys} == {"ConstitutionalLaw", "PoliticalTheory", "LegalStudies"}


def test_swap_pack_replaces_only_designated(torch_pack, profile, tmp_path):
    torch = pytest.importorskip("torch")
    from belay.detector.swap_pack import build

    dest = build(torch_pack, profile, tmp_path / "out")
    assert dest.name == torch_pack.name
    original = {p.relative_to(torch_pack) for p in torch_pack.rglob("*.pt")}
    assert {p.relative_to(dest) for p in dest.rglob("*.pt")} == original
    for rel in original:
        swapped = not (dest / rel).is_symlink()
        assert swapped == (rel.parts[1].split("@")[0] in {"Law", "Courts"})
        if swapped:
            a = torch.load(torch_pack / rel, weights_only=True)
            b = torch.load(dest / rel, weights_only=True)
            assert a.keys() == b.keys()
            assert all(a[k].shape == b[k].shape for k in a)
            assert not torch.equal(a["net.0.weight"], b["net.0.weight"])
    assert (dest / "hierarchy").is_symlink()
    # The source pack is untouched.
    assert not any(p.is_symlink() for p in torch_pack.rglob("*"))


def _signed_log(path: Path, n: int = 3):
    key = key_from_env()
    log = EventLog(path, "ep")
    for i in range(n):
        record = AlertRecord(request_id=f"r-{i}", session_id="s", agent="a", model_hash="sha256:x",
                             alert=Alert(concept="ConstitutionalLaw", score=0.999, token_index=i,
                                         path=["SocialOrganizationGovernance", "ConstitutionalLaw"],
                                         t_token=1.0, t_signed=2.0))
        record.signature = sign(record, key)
        log.write("a", "alert", record.model_dump(mode="json"))
    return key


def test_tamper_log_breaks_one_signature(tmp_path):
    path = tmp_path / "alerts.jsonl"
    key = _signed_log(path)
    result = tamper(path, index=1)
    assert result["before"]["concept"] == "ConstitutionalLaw"
    assert result["after"]["concept"] == "LegalStudies"
    assert result["after"]["path"] == ["SocialOrganizationGovernance", "LegalStudies"]
    events = list(read(path))  # still valid against the schema
    assert len(events) == 3
    assert [verify(e["payload"], key) for e in events] == [True, False, True]


def test_tamper_log_set_fields(tmp_path):
    path = tmp_path / "alerts.jsonl"
    key = _signed_log(path)
    tamper(path, request_id="r-2", changes={"score": 0.1})
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[2]["payload"]["alert"]["score"] == 0.1
    assert not verify(events[2]["payload"], key)
