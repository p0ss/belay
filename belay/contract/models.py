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


class ConceptSummary(BaseModel):
    """
    How one reported concept behaved over a request: the sustained signal.

    A token is covered when HAT scored the concept, or scored one of its
    ancestors (the hierarchy checked that region and found it cold). Covered
    tokens where the concept itself was not scored count as 0 in `mean`.
    """
    concept: str
    covered_tokens: int = Field(ge=0)
    scored_tokens: int = Field(ge=0)
    mean: float = Field(ge=0.0, le=1.0)
    # Share of covered tokens at or above the concept's threshold.
    frac_above: float = Field(ge=0.0, le=1.0)
    peak: float = Field(ge=0.0, le=1.0)
    peak_token: Optional[int] = None


class ConceptPolicy(BaseModel):
    """
    How sensitive the belay is for one concept, chosen per risk class.

    The detector reports the same things for both modes: a signed alert at a
    concept's first crossing of `threshold` in a request, and a summary of
    every request. The mode tells the third party what to alarm on:
    sustained: the concept's signal staying up across requests, per agent
        (e.g. a rising mean or frac_above); a lone alert is a passing thought.
    spike: a single alert, for highly specific concepts where one stray
        crossing is worth pulling the agent out.
    """
    mode: Literal["sustained", "spike"] = "sustained"
    threshold: float = Field(default=0.99, ge=0.0, le=1.0)


class Coverage(BaseModel):
    # Watched concepts that were resident and scored for this request.
    watched: int
    resident_peak: int
    pack: str
    profile: str
    # The detector's run setting: off, proxy, full or wide. None if it does not say.
    watch: Optional[str] = None


class Identity(BaseModel):
    agent_credential: str
    model_hash: str
    pack: str
    profile_hash: str
    # From the certificate the certifier issued; None until certified.
    certificate_id: Optional[str] = None
    certified_until: Optional[str] = None
    # The reported concepts the certificate covers. None: not certified, or a
    # certificate that covers the whole profile.
    certified_concepts: Optional[List[str]] = None


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
    # One per reported concept, every request: the sustained signal.
    summaries: List[ConceptSummary] = Field(default_factory=list)
    # The same, over the input while the model read it (when the detector
    # scores the prompt). Comparing the two separates the prompt's pull from
    # the model's own response. None when not scored.
    prompt_summaries: Optional[List[ConceptSummary]] = None
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
    # Certification is per concept: the concepts that met their criteria, and
    # the profile's concepts that did not. Empty `concepts` (older
    # certificates) means the whole profile.
    concepts: List[str] = Field(default_factory=list)
    uncertified: List[str] = Field(default_factory=list)
    # HMAC-SHA256 by the certifier key.
    signature: str = ""


class SummaryRecord(BaseModel):
    """One request's signed summaries, as the detector writes them to the log and the stream."""
    request_id: str
    session_id: str
    agent: str
    summaries: List[ConceptSummary]
    tokens: int
    t_start: float
    # Over the input, when the detector scores the prompt; None otherwise.
    prompt_summaries: Optional[List[ConceptSummary]] = None
    t_end: float
    model_hash: str
    # HMAC-SHA256 by the lab key over the canonical encoding of every other field.
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
