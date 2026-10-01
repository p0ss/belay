"""
Build the untrained pack for the `swap` tamper mode.

A copy of the lens pack in which every probe file of the designated concepts
(the profile's watched concepts) is replaced by an untrained probe: the same
file name, keys and shapes, with weights drawn the way torch initialises a
fresh nn.Linear. Everything else (other lenses, hierarchy, calibration) is a
symlink to the original, so the swapped pack looks the same on disk apart from
those files. The source pack is never modified.

    uv run python -m belay.detector.swap_pack \
        [--pack PACK_DIR] [--profile profiles/proxy-redlines.txt] [--out runs/untrained-packs]
"""

from __future__ import annotations

import argparse
import math
import shutil
from pathlib import Path
from typing import List

from .pack import Hierarchy, lens_files, read_profile, watched_keys

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACK = Path("/var/home/poss/Documents/Code/HatCatDev/lens_packs/gemma-4-e4b-it_university-v3-contrasts-bands")
DEFAULT_PROFILE = ROOT / "profiles" / "proxy-redlines.txt"
DEFAULT_OUT = ROOT / "runs" / "untrained-packs"


def _untrained(state_dict: dict, generator) -> dict:
    import torch

    out = {}
    for name, tensor in state_dict.items():
        if not torch.is_floating_point(tensor):
            out[name] = tensor.clone()
            continue
        if tensor.dim() >= 2:
            fan_in = tensor[0].numel()
        else:
            # A bias: bounded by its layer's fan-in, as nn.Linear does.
            weight = state_dict.get(name.rsplit(".", 1)[0] + ".weight") if "." in name else None
            fan_in = weight[0].numel() if weight is not None and weight.dim() >= 2 else max(tensor.numel(), 1)
        bound = 1 / math.sqrt(fan_in)
        out[name] = (torch.rand(tensor.shape, generator=generator, dtype=torch.float32) * 2 - 1).mul_(bound).to(tensor.dtype)
    return out


def build(pack: Path, profile: Path, out: Path, seed: int = 0, overwrite: bool = False) -> Path:
    import torch

    pack, out = Path(pack).resolve(), Path(out)
    dest = out / pack.name
    if dest.exists():
        if not overwrite:
            return dest
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    lensed = lens_files(pack)
    designated = watched_keys(read_profile(profile), lensed, Hierarchy(pack))
    swapped = {f.resolve() for k in designated for f in lensed[k]}
    if not swapped:
        raise SystemExit(f"no lens files for the concepts in {profile}")

    generator = torch.Generator().manual_seed(seed)
    written: List[str] = []
    for item in sorted(pack.iterdir()):
        target = dest / item.name
        if item.is_dir() and item.name.startswith("layer"):
            target.mkdir()
            for f in sorted(item.iterdir()):
                if f.resolve() in swapped:
                    state = torch.load(f, map_location="cpu", weights_only=True)
                    torch.save(_untrained(state, generator), target / f.name)
                    written.append(f"{item.name}/{f.name}")
                else:
                    (target / f.name).symlink_to(f)
        else:
            target.symlink_to(item)
    (dest / "SWAPPED.txt").write_text(
        "Untrained probes (random weights, same names) for the designated concepts:\n"
        + "".join(f"{w}\n" for w in written)
    )
    return dest


def main() -> None:
    p = argparse.ArgumentParser(description="Build the untrained pack for --tamper swap")
    p.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()
    dest = build(args.pack, args.profile, args.out, args.seed, args.overwrite)
    print(dest)
    print((dest / "SWAPPED.txt").read_text(), end="")


if __name__ == "__main__":
    main()
