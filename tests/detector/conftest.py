import json
from pathlib import Path

import pytest

# A tiny band pack:  Root(layer0) -> Law(layer1) -> Courts(layer2), Root -> Chem(layer1)
PROBES = {
    "layer0": ["Root@L0.pt", "Root@L4.pt"],
    "layer1": ["Law@L2.pt", "Law@L5.pt", "Chem@L1.pt"],
    "layer2": ["Courts@L3.pt"],
}
CHILD_TO_PARENT = {"Law:1": "Root:0", "Chem:1": "Root:0", "Courts:2": "Law:1", "Unlensed:2": "Law:1"}
HIDDEN = 16


@pytest.fixture
def fake_pack(tmp_path) -> Path:
    pack = tmp_path / "fake-pack"
    for layer, files in PROBES.items():
        (pack / layer).mkdir(parents=True)
        for name in files:
            (pack / layer / name).write_bytes(b"")
    (pack / "hierarchy").mkdir()
    (pack / "hierarchy" / "hierarchy.json").write_text(json.dumps(
        {"child_to_parent": CHILD_TO_PARENT, "root_concepts": ["Root:0"]}))
    return pack


@pytest.fixture
def torch_pack(fake_pack) -> Path:
    """The fake pack with real SimpleMLP state dicts in its probe files."""
    torch = pytest.importorskip("torch")
    lens_types = pytest.importorskip("headspace.monitoring.lens_types")
    torch.manual_seed(0)
    for layer, files in PROBES.items():
        for name in files:
            torch.save(lens_types.SimpleMLP(HIDDEN).state_dict(), fake_pack / layer / name)
    return fake_pack


@pytest.fixture
def profile(tmp_path) -> Path:
    p = tmp_path / "profile.txt"
    p.write_text("# test profile\nLaw\n")
    return p
