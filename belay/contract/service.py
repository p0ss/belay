"""
The detector's HTTP surface, shared by the stub and the real lab-side
detector so both honour the contract the same way. A backend does the
generation and monitoring; this module signs, logs and streams.

Endpoints:
    POST /generate      GenerateRequest -> GenerateResponse
    GET  /alerts        server-sent events, one signed AlertRecord per event
    POST /certificate   install a Certificate issued by the certifier
    GET  /health        liveness, and the identity the detector claims
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Protocol, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from .events import EventLog
from .models import Alert, AlertRecord, Certificate, Coverage, GenerateRequest, GenerateResponse, Identity
from .signing import credential, key_from_env, sign


@dataclass
class RawAlert:
    concept: str
    score: float
    token_index: int
    path: List[str]
    t_token: float


@dataclass
class BackendResult:
    completion: str
    tokens: int
    alerts: List[RawAlert]
    watched: int
    resident_peak: int
    overhead_ms: float


class Backend(Protocol):
    """What a detector must provide. `generate` may be called from many threads."""
    model_hash: str
    pack: str
    profile: str
    profile_hash: str

    def generate(self, request: GenerateRequest) -> BackendResult: ...


@dataclass
class _Broadcast:
    subscribers: List[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=10_000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, item: dict) -> None:
        with self.lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass


def create_app(backend: Backend, log_path: Path, key: Optional[bytes] = None) -> FastAPI:
    key = key or key_from_env()
    app = FastAPI(title="Belay detector")
    stream = _Broadcast()
    logs: Dict[str, EventLog] = {}
    logs_lock = threading.Lock()
    state: Dict[str, Optional[Certificate]] = {"certificate": None}

    def log_for(episode: str) -> EventLog:
        with logs_lock:
            if episode not in logs:
                logs[episode] = EventLog(log_path, episode)
            return logs[episode]

    def identity(agent: str) -> Identity:
        cert = state["certificate"]
        return Identity(
            agent_credential=credential(agent, backend.model_hash, backend.pack, backend.profile_hash, key),
            model_hash=backend.model_hash,
            pack=backend.pack,
            profile_hash=backend.profile_hash,
            certificate_id=cert.certificate_id if cert else None,
            certified_until=cert.certified_until if cert else None,
        )

    @app.post("/generate", response_model=GenerateResponse)
    def generate(req: GenerateRequest) -> GenerateResponse:
        log = log_for(req.episode or "detector")
        result = backend.generate(req)
        alerts: List[Alert] = []
        records: List[AlertRecord] = []
        for raw in result.alerts:
            alert = Alert(
                concept=raw.concept, score=min(max(raw.score, 0.0), 1.0), token_index=raw.token_index,
                path=raw.path, t_token=raw.t_token, t_signed=time.time(),
            )
            record = AlertRecord(
                request_id=req.request_id, session_id=req.session_id, agent=req.agent,
                alert=alert, model_hash=backend.model_hash,
            )
            record.signature = sign(record, key)
            alerts.append(alert)
            records.append(record)

        response = GenerateResponse(
            request_id=req.request_id, session_id=req.session_id, agent=req.agent,
            completion=result.completion, tokens=result.tokens, alerts=alerts,
            coverage=Coverage(watched=result.watched, resident_peak=result.resident_peak,
                              pack=backend.pack, profile=backend.profile),
            overhead_ms=result.overhead_ms, identity=identity(req.agent),
        )
        response.signature = sign(response, key)

        for record in records:
            payload = record.model_dump(mode="json")
            log.write(req.agent, "alert", payload, t=record.alert.t_signed)
            stream.publish(payload)
        return response

    @app.get("/alerts")
    def alerts() -> StreamingResponse:
        q = stream.subscribe()

        def events() -> Iterator[str]:
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                stream.unsubscribe(q)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/certificate")
    def install_certificate(cert: Certificate) -> dict:
        mismatch: List[Tuple[str, str, str]] = [
            (name, got, want)
            for name, got, want in [
                ("model_hash", cert.model_hash, backend.model_hash),
                ("pack", cert.pack, backend.pack),
                ("profile_hash", cert.profile_hash, backend.profile_hash),
            ]
            if got != want
        ]
        if mismatch:
            raise HTTPException(409, detail={"mismatch": mismatch})
        state["certificate"] = cert
        return {"installed": cert.certificate_id}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "identity": identity("health").model_dump(mode="json")}

    return app
