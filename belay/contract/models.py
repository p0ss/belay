"""
The detector API: the only thing that crosses the boundary between the lab
side and everyone else. See docs/contract.md.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class GenerateRequest(BaseModel):
    request_id: str
    session_id: str
    agent: str
    messages: List[Message]
    max_tokens: int = 256
    # Episode the request belongs to, for the detector's alert log. None for
    # traffic outside an episode (such as certification).
    episode: Optional[str] = None


class Alert(BaseModel):
    concept: str
    score: float = Field(ge=0.0, le=1.0)
    token_index: int = Field(ge=0)
    path: List[str] = Field(default_factory=list)
    # Unix seconds: when the token was generated, and when the alert was signed.
    t_token: float
    t_signed: float


class Coverage(BaseModel):
    # Watched concepts that were resident and scored for this request.
    watched: int
    resident_peak: int
    pack: str
    profile: str


class Identity(BaseModel):
    agent_credential: str
    model_hash: str
    pack: str
    profile_hash: str
    # From the certificate the certifier issued; None until certified.
    certificate_id: Optional[str] = None
    certified_until: Optional[str] = None


class GenerateResponse(BaseModel):
    request_id: str
    session_id: str
    agent: str
    completion: str
    tokens: int
    alerts: List[Alert]
    coverage: Coverage
    overhead_ms: float
    identity: Identity
    # HMAC-SHA256 by the lab key over the canonical encoding of every other
    # field (see belay.contract.signing).
    signature: str = ""


class Certificate(BaseModel):
    """Issued by the certifier, installed on the detector with POST /certificate."""
    certificate_id: str
    model_hash: str
    pack: str
    profile_hash: str
    issued: str
    certified_until: str
    # Next pre-agreed re-certification point, in words.
    recertify_at: str
    criteria: dict = Field(default_factory=dict)
    results: dict = Field(default_factory=dict)
    # HMAC-SHA256 by the certifier key.
    signature: str = ""


class AlertRecord(BaseModel):
    """One signed alert, as the detector writes it to the log and the stream."""
    request_id: str
    session_id: str
    agent: str
    alert: Alert
    model_hash: str
    # HMAC-SHA256 by the lab key over the canonical encoding of every other field.
    signature: str = ""
