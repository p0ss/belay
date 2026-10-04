"""
Signatures. An HMAC is enough for the demonstration: the log keeper holds the
lab key's verifier, and any change to a signed record breaks its signature.

The canonical encoding is JSON with sorted keys, no whitespace, UTF-8, and the
`signature` field removed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Mapping, Union

from pydantic import BaseModel

LAB_KEY_ENV = "BELAY_LAB_KEY"
CERTIFIER_KEY_ENV = "BELAY_CERTIFIER_KEY"

Signable = Union[BaseModel, Mapping[str, Any]]


def _as_dict(record: Signable) -> dict:
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json")
    return dict(record)


def canonical(record: Signable) -> bytes:
    data = _as_dict(record)
    data.pop("signature", None)
    # Optional top-level fields that are absent (None) are left out, so a
    # record signed before such a field existed still verifies.
    data = {k: v for k, v in data.items() if v is not None}
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def key_from_env(name: str = LAB_KEY_ENV) -> bytes:
    value = os.environ.get(name)
    if not value:
        # A fixed development key, so the stub and tests run without setup.
        value = f"belay-dev-{name.lower()}"
    return value.encode("utf-8")


def sign(record: Signable, key: bytes) -> str:
    return hmac.new(key, canonical(record), hashlib.sha256).hexdigest()


def verify(record: Signable, key: bytes) -> bool:
    signature = _as_dict(record).get("signature") or ""
    return hmac.compare_digest(signature, sign(record, key))


def credential(agent: str, model_hash: str, pack: str, profile_hash: str, key: bytes) -> str:
    """The agent credential: binds an agent to the certified model, pack and profile."""
    return sign({"agent": agent, "model_hash": model_hash, "pack": pack, "profile_hash": profile_hash}, key)
