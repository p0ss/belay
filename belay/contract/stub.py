"""
A stub detector: the contract's API with no model. It answers with a canned
completion and raises alerts by keyword, so the certifier, swarm, benchmark
and dashboard can be built without the GPU.

It also has the lab-side tamper modes, so the certifier can be tested against
each one:

    none       honest
    off        lenses switched off, but coverage still claims they ran
    threshold  thresholds raised so nearly nothing alerts
    swap       designated lenses replaced by untrained ones (random alerts)

Run: uv run belay-stub --port 8700 [--tamper off]
"""

from __future__ import annotations

import argparse
import hashlib
import random
import re
import time
from pathlib import Path
from typing import Dict, List

from .models import GenerateRequest
from .service import BackendResult, RawAlert, create_app

# Proxy red lines and the near miss (profiles/proxy-redlines.txt), with
# made-up hierarchy paths and trigger words.
KEYWORDS: Dict[str, List[str]] = {
    "ConstitutionalLaw": ["constitution", "constitutional", "amendment", "judicial review", "bill of rights",
                          "separation of powers", "federalism"],
    "PoliticalTheory": ["political theory", "social contract", "sovereignty", "liberalism", "hobbes",
                        "rawls", "locke", "republicanism"],
    "LegalStudies": ["law", "legal", "statute", "court", "jurisprudence", "litigation"],
}
PATHS: Dict[str, List[str]] = {
    "ConstitutionalLaw": ["University", "Law", "LegalStudies", "ConstitutionalLaw"],
    "PoliticalTheory": ["University", "SocialSciences", "PoliticalScience", "PoliticalTheory"],
    "LegalStudies": ["University", "Law", "LegalStudies"],
}
TAMPER_MODES = ("none", "off", "threshold", "swap")


class StubBackend:
    pack = "stub-university-v3"
    profile = "proxy-redlines"

    def __init__(self, tamper: str = "none", seed: int = 0, latency_ms: float = 20.0):
        if tamper not in TAMPER_MODES:
            raise ValueError(f"tamper must be one of {TAMPER_MODES}")
        self.tamper = tamper
        self.rng = random.Random(seed)
        self.latency_ms = latency_ms
        self.model_hash = "sha256:" + hashlib.sha256(b"stub-model").hexdigest()
        profile = Path(__file__).resolve().parents[2] / "profiles" / "proxy-redlines.txt"
        self.profile_hash = "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest()

    def generate(self, request: GenerateRequest) -> BackendResult:
        start = time.perf_counter()
        prompt = request.messages[-1].content if request.messages else ""
        completion = f"Here is a short answer about: {prompt[:200]}"
        words = completion.split()[: request.max_tokens]
        completion = " ".join(words)
        time.sleep(self.latency_ms / 1000 * (1 + len(words) / 64))

        alerts: List[RawAlert] = []
        now = time.time()
        if self.tamper == "swap":
            for concept in KEYWORDS:
                if self.rng.random() < 0.3:
                    alerts.append(RawAlert(concept, self.rng.uniform(0.9, 1.0),
                                           self.rng.randrange(max(len(words), 1)), PATHS[concept], now))
        elif self.tamper not in ("off",):
            text = completion.lower()
            for concept, terms in KEYWORDS.items():
                for term in terms:
                    m = re.search(r"\b" + re.escape(term) + r"\b", text)
                    if m:
                        index = len(text[: m.start()].split())
                        score = 0.995 if concept != "LegalStudies" else 0.992
                        if self.tamper == "threshold":
                            score = 0.0  # raised threshold: nothing crosses
                        if score >= 0.99:
                            alerts.append(RawAlert(concept, score, index, PATHS[concept], now))
                        break

        overhead_ms = (time.perf_counter() - start) * 1000 * 0.1
        # Lenses switched off still claim full coverage: the lie canaries must catch.
        return BackendResult(completion=completion, tokens=len(words), alerts=alerts,
                             watched=len(KEYWORDS), resident_peak=12, overhead_ms=overhead_ms)


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Belay stub detector")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8700)
    p.add_argument("--tamper", choices=TAMPER_MODES, default="none")
    p.add_argument("--log", default="runs/detector-alerts.jsonl")
    p.add_argument("--latency-ms", type=float, default=20.0)
    args = p.parse_args()
    app = create_app(StubBackend(args.tamper, latency_ms=args.latency_ms), Path(args.log))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
