"""
Sending requests that look like ordinary traffic. Request ids are opaque and
shaped like the swarm's ("<task_id>.<step>"), session and agent ids are drawn
from the same kind of pool the swarm uses, so the lab side cannot pick out
certification or canary cases by their envelope.
"""

from __future__ import annotations

import random
import secrets
import time
from dataclasses import dataclass
from typing import List, Optional

import httpx


def opaque_task_id(rng: Optional[random.Random] = None) -> str:
    if rng is None:
        return secrets.token_hex(6)
    return "%012x" % rng.getrandbits(48)


@dataclass
class Sent:
    request: dict
    raw: Optional[dict]
    status: int
    error: Optional[str]
    t_sent: float
    t_received: float


class Traffic:
    def __init__(self, client: httpx.Client, rng: random.Random, agents: int = 16,
                 episode: Optional[str] = None, max_tokens: int = 256):
        self.client = client
        self.rng = rng
        self.agents = agents
        self.episode = episode
        self.max_tokens = max_tokens
        self.sessions = ["s-%06x" % rng.getrandbits(24) for _ in range(agents)]

    def envelope(self, messages: List[dict], step: int = 0, task_id: Optional[str] = None) -> dict:
        i = self.rng.randrange(self.agents)
        task_id = task_id or opaque_task_id(self.rng)
        req = {"request_id": f"{task_id}.{step}", "session_id": self.sessions[i], "agent": f"agent-{i}",
               "messages": messages, "max_tokens": self.max_tokens}
        if self.episode:
            req["episode"] = self.episode
        return req

    def send(self, request: dict) -> Sent:
        t0 = time.time()
        try:
            r = self.client.post("/generate", json=request)
            raw = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
            err = None if r.status_code == 200 else f"HTTP {r.status_code}"
            return Sent(request, raw if r.status_code == 200 else None, r.status_code, err, t0, time.time())
        except (httpx.HTTPError, ValueError) as e:
            return Sent(request, None, 0, f"{type(e).__name__}: {e}", t0, time.time())


def make_client(url: str, timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(base_url=url.rstrip("/"), timeout=timeout)
