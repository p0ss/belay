"""Identity hashes: the model's weight files and the watch profile."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, List, Optional

WEIGHT_PATTERNS = ("*.safetensors", "*.bin", "*.pt", "*.gguf")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def profile_hash(path: Path) -> str:
    """What a certificate binds: the report profile and its policy (belay.contract.policy.digest)."""
    from belay.contract.policy import digest

    return digest(Path(path))


def model_dir(model_id: str) -> Path:
    """The local snapshot of a model: a directory, or a Hugging Face id already in the cache.

    Never downloads."""
    p = Path(model_id)
    if p.is_dir():
        return p
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(model_id, local_files_only=True))


def weight_files(directory: Path) -> List[Path]:
    files = sorted({f for pattern in WEIGHT_PATTERNS for f in Path(directory).glob(pattern)})
    if not files:
        raise FileNotFoundError(f"no weight files in {directory}")
    return files


def hash_files(files: Iterable[Path], cache: Optional[Path] = None, chunk: int = 1 << 24) -> str:
    """sha256 over the files' bytes in order (file names are not hashed).

    Hashing 15 GB takes a while, so the result can be cached against each file's
    resolved path, size and modification time."""
    files = [Path(f) for f in files]
    stamp = [[str(f.resolve()), f.stat().st_size, f.stat().st_mtime_ns] for f in files]
    if cache is not None and cache.exists():
        try:
            saved = json.loads(cache.read_text())
            if saved.get("files") == stamp:
                return saved["hash"]
        except (ValueError, KeyError):
            pass
    h = hashlib.sha256()
    for f in files:
        with f.open("rb") as fh:
            while block := fh.read(chunk):
                h.update(block)
    digest = "sha256:" + h.hexdigest()
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"files": stamp, "hash": digest}))
    return digest
