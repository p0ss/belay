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
# A stand-in for the wide pack:  Mind(layer0) -> Deception(layer1), Mind -> Tent(layer1)
WIDE_PROBES = {"layer0": ["Mind@L5.pt"], "layer1": ["Deception@L5.pt", "Tent@L5.pt"]}
WIDE_CHILD_TO_PARENT = {"Deception:1": "Mind:0", "Tent:1": "Mind:0"}
HIDDEN = 16


def _write_pack(pack: Path, probes: dict, child_to_parent: dict, roots: list) -> Path:
    for layer, files in probes.items():
        (pack / layer).mkdir(parents=True)
        for name in files:
            (pack / layer / name).write_bytes(b"")
    parent_to_children = {}
    for child, parent in child_to_parent.items():
        parent_to_children.setdefault(parent, []).append(child)
    (pack / "hierarchy").mkdir()
    (pack / "hierarchy" / "hierarchy.json").write_text(json.dumps(
        {"child_to_parent": child_to_parent, "parent_to_children": parent_to_children, "root_concepts": roots}))
    return pack


def _fill(pack: Path, probes: dict, seed: int) -> Path:
    """Real SimpleMLP state dicts in a pack's probe files."""
    torch = pytest.importorskip("torch")
    lens_types = pytest.importorskip("headspace.monitoring.lens_types")
    torch.manual_seed(seed)
    for layer, files in probes.items():
        for name in files:
            torch.save(lens_types.SimpleMLP(HIDDEN).state_dict(), pack / layer / name)
    return pack


@pytest.fixture
def fake_pack(tmp_path) -> Path:
    return _write_pack(tmp_path / "fake-pack", PROBES, CHILD_TO_PARENT, ["Root:0"])


@pytest.fixture
def torch_pack(fake_pack) -> Path:
    """The fake pack with real SimpleMLP state dicts in its probe files."""
    return _fill(fake_pack, PROBES, 0)


@pytest.fixture
def wide_pack(tmp_path) -> Path:
    """A second pack for --run wide, with concepts of its own."""
    pack = _write_pack(tmp_path / "wide-pack", WIDE_PROBES, WIDE_CHILD_TO_PARENT, ["Mind:0"])
    return _fill(pack, WIDE_PROBES, 1)


@pytest.fixture
def profile(tmp_path) -> Path:
    p = tmp_path / "profile.txt"
    p.write_text("# test profile\nLaw\n")
    return p
