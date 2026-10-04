"""
The swarm orchestrator: N concurrent agent loops in one sealed text world,
each using the detector as its model through POST /generate.

    uv run belay-swarm --detector-url http://127.0.0.1:8700 --agents 16 --steps 40 --episode ep-001

Every request carries request_id "<task_id>.<step>", the agent's session id,
the agent id and the episode. Every action carries the agent's identity
credential from the detector's last response. Everything goes to the contract
event log, which is validated at the end.

Stops on: the per-agent step limit, the time limit, an empty task queue,
the kill file appearing, or Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from belay.contract.events import EventLog, read

from . import agent as policy
from .fields import designated
from .tasks import Task, generate, load
from .world import LURES, World


class TaskQueue:
    def __init__(self, tasks: List[Task]):
        self._tasks = list(tasks)
        self._lock = threading.Lock()

    def pop(self, accept: Optional[Callable[[Task], bool]] = None) -> Optional[Task]:
        """The next task, or the next one `accept` takes."""
        with self._lock:
            for i, task in enumerate(self._tasks):
                if accept is None or accept(task):
                    return self._tasks.pop(i)
            return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)


def mix(tasks: List[Task], extra: List[Task], rng: random.Random) -> List[Task]:
    """Insert `extra` at random positions. Nothing marks which tasks they were."""
    out = list(tasks)
    for t in extra:
        out.insert(rng.randint(0, len(out)), t)
    return out


@dataclass
class Config:
    detector_url: str = "http://127.0.0.1:8700"
    agents: int = 16
    steps: int = 40            # per agent, across all its tasks
    task_steps: int = 8        # per task, after which the agent submits
    time_limit: float = 600.0  # seconds, for the whole episode
    max_tokens: int = 192
    episode: str = "ep-001"
    out: Optional[Path] = None
    kill_file: Optional[Path] = None
    pressure: bool = False
    retries: int = 2
    timeout: float = 120.0
    # The drift scenario: `drift` agents get only ordinary (non-danger) tasks,
    # and from their step `drift_after + 1` on, the world pulls them toward
    # `drift_concept` (default: alternate between the red lines in LURES).
    drift: int = 0
    drift_after: int = 10
    drift_concept: Optional[str] = None


@dataclass
class Stats:
    steps: Counter = field(default_factory=Counter)
    submitted: int = 0
    parsed: int = 0
    fallback: int = 0
    errors: int = 0
    alerts: Counter = field(default_factory=Counter)
    stopped_by: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


class Swarm:
    def __init__(self, cfg: Config, tasks: List[Task], client: Optional[httpx.Client] = None):
        self.cfg = cfg
        self.out = cfg.out or Path("runs/swarm") / f"{cfg.episode}.jsonl"
        if self.out.exists():
            raise FileExistsError(f"{self.out} exists; choose another --episode or --out")
        self.kill_file = cfg.kill_file or self.out.with_suffix(".kill")
        self.log = EventLog(self.out, cfg.episode)
        self.world = World(self.log, pressure=cfg.pressure)
        self.queue = TaskQueue(tasks)
        self.client = client or httpx.Client(base_url=cfg.detector_url, timeout=cfg.timeout)
        self.stop = threading.Event()
        self.stats = Stats()
        self.deadline = 0.0
        self.drifted = self._pick_drifters()

    def _pick_drifters(self) -> Dict[str, str]:
        """Agent -> the danger area it will be pulled toward. Chosen from the episode id, so reproducible."""
        n = max(0, min(self.cfg.drift, self.cfg.agents))
        if not n:
            return {}
        concepts = [self.cfg.drift_concept] if self.cfg.drift_concept else sorted(LURES)
        unknown = [c for c in concepts if c not in LURES]
        if unknown:
            raise ValueError(f"no lure for {unknown}; choose from {sorted(LURES)}")
        names = [f"agent-{i:02d}" for i in range(1, self.cfg.agents + 1)]
        chosen = sorted(random.Random(f"drift-{self.cfg.episode}").sample(names, n))
        return {name: concepts[i % len(concepts)] for i, name in enumerate(chosen)}

    # --- stopping ----------------------------------------------------------

    def should_stop(self) -> bool:
        if self.stop.is_set():
            return True
        reason = ""
        if self.kill_file.exists():
            reason = f"kill file {self.kill_file}"
        elif time.time() > self.deadline:
            reason = "time limit"
        if reason:
            self.halt(reason)
            return True
        return False

    def halt(self, reason: str) -> None:
        with self.stats.lock:
            if not self.stats.stopped_by:
                self.stats.stopped_by = reason
        self.stop.set()

    # --- the model ---------------------------------------------------------

    def generate(self, agent: str, session: str, request_id: str, messages: List[dict]) -> Optional[dict]:
        body = {"request_id": request_id, "session_id": session, "agent": agent, "messages": messages,
                "max_tokens": self.cfg.max_tokens, "episode": self.cfg.episode}
        for attempt in range(self.cfg.retries + 1):
            if self.stop.is_set():
                return None
            try:
                r = self.client.post("/generate", json=body)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                err = f"{type(e).__name__}: {e}"
                time.sleep(min(2.0, 0.2 * (attempt + 1)))
        with self.stats.lock:
            self.stats.errors += 1
        print(f"[{agent}] detector request {request_id} failed: {err}", file=sys.stderr)
        return None

    # --- one agent ---------------------------------------------------------

    def run_agent(self, name: str) -> None:
        session = f"s-{self.cfg.episode}-{name}"
        credential = ""
        self.world.join(name)
        used = 0
        lure = self.drifted.get(name)
        # Drifting agents are assigned ordinary tasks only: their drift is off task by construction.
        accept = (lambda t: t.field not in designated()) if lure else None
        while used < self.cfg.steps and not self.should_stop():
            task = self.queue.pop(accept)
            if task is None:
                return
            self.world.assign(name, task)
            history: List[str] = []
            for step in range(1, self.cfg.task_steps + 1):
                if used >= self.cfg.steps or self.should_stop():
                    return
                used += 1
                request_id = f"{task.task_id}.{step}"
                if lure and used > self.cfg.drift_after:
                    self.world.nudge(name, lure, request_id)
                messages = policy.prompt(task, step, self.cfg.task_steps, self.world.observe(name), history,
                                         note=self.world.task_note(name))
                response = self.generate(name, session, request_id, messages)
                completion = ""
                if response is not None:
                    completion = response.get("completion", "")
                    credential = (response.get("identity") or {}).get("agent_credential") or credential
                    with self.stats.lock:
                        for a in response.get("alerts", []):
                            self.stats.alerts[(task.field, a.get("concept"))] += 1
                reasoning = {"text": completion, "visible_to_monitor": True,
                             "request_id": request_id, "session_id": session}
                if response is None:
                    reasoning["error"] = "detector unavailable"
                else:
                    # The signed response, so the certifier can check signatures,
                    # coverage and identity on every call, canaries included.
                    reasoning["response"] = response
                self.log.write(name, "reasoning", reasoning)

                parsed = policy.parse(completion)
                last_step = step == self.cfg.task_steps or used == self.cfg.steps
                if parsed and not (last_step and parsed[0] != "submit"):
                    action, args = parsed
                    with self.stats.lock:
                        self.stats.parsed += 1
                else:
                    action, args = (policy.final_action(task, completion) if last_step
                                    else policy.default_action(task, step, completion,
                                                               lure=self.world.lure_topic(name)))
                    with self.stats.lock:
                        self.stats.fallback += 1
                args = policy.fix_args(task, action, args, completion)
                args["credential"] = credential
                out = self.world.act(name, action, args, extra={"request_id": request_id})
                with self.stats.lock:
                    self.stats.steps[name] += 1
                detail = out.detail if isinstance(out.detail, str) else json.dumps(out.detail, ensure_ascii=False)
                history.append(f"{action} -> {'ok' if out.ok else 'refused'}: {detail[:200]}")
                if out.submitted:
                    with self.stats.lock:
                        self.stats.submitted += 1
                    break

    # --- the episode -------------------------------------------------------

    def run(self, on_start: Optional[Callable[[], None]] = None) -> Stats:
        if self.kill_file.exists():
            raise RuntimeError(f"kill file {self.kill_file} exists; remove it to run")
        self.deadline = time.time() + self.cfg.time_limit
        names = [f"agent-{i:02d}" for i in range(1, self.cfg.agents + 1)]
        threads = [threading.Thread(target=self._guard, args=(n,), name=n, daemon=True) for n in names]
        for t in threads:
            t.start()
        if on_start:
            on_start()
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.2)
                self.should_stop()
        except KeyboardInterrupt:
            self.halt("interrupted")
            for t in threads:
                t.join(timeout=self.cfg.timeout)
        if not self.stats.stopped_by:
            self.stats.stopped_by = "task queue empty" if len(self.queue) == 0 else "step limit"
        return self.stats

    def _guard(self, name: str) -> None:
        try:
            self.run_agent(name)
        except Exception as e:  # one agent failing must not take down the rest
            print(f"[{name}] crashed: {type(e).__name__}: {e}", file=sys.stderr)
            with self.stats.lock:
                self.stats.errors += 1

    def validate(self) -> int:
        """Validate every event in the log; returns the count."""
        return sum(1 for _ in read(self.out))


def summary(swarm: Swarm, n_events: int) -> dict:
    s = swarm.stats
    kinds = Counter(e["kind"] for e in read(swarm.out, validate_each=False))
    crossings = Counter(e["payload"]["redline"] for e in read(swarm.out, validate_each=False)
                        if e["kind"] == "label" and e["payload"].get("basis") == "crossing")
    return {
        "episode": swarm.cfg.episode, "log": str(swarm.out), "events": n_events, "kinds": dict(kinds),
        "agents": swarm.cfg.agents, "steps": sum(s.steps.values()), "submitted": s.submitted,
        "parsed_actions": s.parsed, "fallback_actions": s.fallback, "errors": s.errors,
        "crossings": dict(crossings), "stopped_by": s.stopped_by,
        "alerts_by_field": {f"{f} -> {c}": n for (f, c), n in sorted(s.alerts.items())},
        "drifted": swarm.drifted,
    }


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Belay swarm runner: concurrent agents in a sealed text world")
    p.add_argument("--detector-url", default="http://127.0.0.1:8700")
    p.add_argument("--agents", type=int, default=16)
    p.add_argument("--tasks", type=Path, default=None, help="tasks JSONL (default: generate --n-tasks)")
    p.add_argument("--n-tasks", type=int, default=None, help="tasks to generate when --tasks is not given")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--canaries", type=Path, default=None, help="extra tasks JSONL mixed in at random positions")
    p.add_argument("--steps", type=int, default=40, help="step limit per agent")
    p.add_argument("--task-steps", type=int, default=8, help="steps per task before the agent must submit")
    p.add_argument("--time-limit", type=float, default=600.0, help="seconds for the whole episode")
    p.add_argument("--max-tokens", type=int, default=192)
    p.add_argument("--episode", default=None, help="episode id (default: ep-<unix time>)")
    p.add_argument("--out", type=Path, default=None, help="event log (default: runs/swarm/<episode>.jsonl)")
    p.add_argument("--kill-file", type=Path, default=None, help="stop all agents when this file exists "
                   "(default: <out>.kill)")
    p.add_argument("--pressure", action="store_true", help="seed the noticeboard with posts that normalise crossing")
    p.add_argument("--drift", type=int, default=0,
                   help="agents on ordinary tasks that the world starts pulling toward a danger area")
    p.add_argument("--drift-after", type=int, default=10,
                   help="steps each drifting agent takes before the first nudge")
    p.add_argument("--drift-concept", choices=sorted(LURES), default=None,
                   help="danger area the drifting agents are pulled toward (default: alternate)")
    args = p.parse_args(argv)

    episode = args.episode or f"ep-{int(time.time())}"
    rng = random.Random(args.seed)
    tasks = load(args.tasks) if args.tasks else generate(args.n_tasks or args.agents * 4, seed=args.seed)
    rng.shuffle(tasks)
    if args.canaries:
        tasks = mix(tasks, load(args.canaries), rng)

    cfg = Config(detector_url=args.detector_url, agents=args.agents, steps=args.steps, task_steps=args.task_steps,
                 time_limit=args.time_limit, max_tokens=args.max_tokens, episode=episode, out=args.out,
                 kill_file=args.kill_file, pressure=args.pressure, drift=args.drift,
                 drift_after=args.drift_after, drift_concept=args.drift_concept)
    swarm = Swarm(cfg, tasks)
    signal.signal(signal.SIGTERM, lambda *_: swarm.halt("terminated"))
    print(f"episode {episode}: {cfg.agents} agents, {len(tasks)} tasks, log {swarm.out}, "
          f"kill file {swarm.kill_file}", file=sys.stderr)
    if swarm.drifted:
        print(f"drift after step {cfg.drift_after}: " + ", ".join(f"{a} -> {c}" for a, c in swarm.drifted.items()),
              file=sys.stderr)
    swarm.run()
    n = swarm.validate()
    print(json.dumps(summary(swarm, n), indent=1))


if __name__ == "__main__":
    main()
