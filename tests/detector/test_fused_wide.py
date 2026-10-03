"""The fused wide scorer gives HAT's SimpleMLP numbers (bf16 rounding aside)."""

from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("headspace")

from belay.detector.backend import WIDE_PACK, FusedWidePack  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(WIDE_PACK).is_dir(), reason="First Light pack not present")


def test_fused_matches_simple_mlp():
    from headspace.monitoring.lens_types import SimpleMLP

    f = FusedWidePack(Path(WIDE_PACK), "cpu", budget_mb=20)
    assert 0 < f.total < f.total_in_pack and f.weight_mb <= 20
    torch.manual_seed(0)
    h = torch.randn(3, f.hidden_dim) * 3
    got = f.read_batch(h)
    assert got.shape == (3, f.total)
    for j in (0, f.total // 2, f.total - 1):
        m = SimpleMLP(f.hidden_dim)
        m.load_state_dict(torch.load(f.manager.concept_metadata[f.keys[j]].activation_lens_path, map_location="cpu"))
        m.eval()
        assert torch.allclose(m(h).squeeze(-1), got[:, j], atol=0.01)
