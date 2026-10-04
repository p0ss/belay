"""
Send the agents' own prompts straight to the model and keep what it says.

For each monitored concept this builds the turn a group B agent sees (a
positive task with the negative world's note, the latest peer posts in view,
and a read of the noticeboard in its recent history), at normal and strong
strength, plus the same task in an ordinary world as the control. Each goes
through the detector with a longer completion, so refusals and deflection
have room to show. Writes JSON lines (prompt, completion, signed summaries)
and a Markdown file to read.

    python -m belay.swarm.probe_pass --detector-url URL --concepts C1,C2 --out runs/x/probe_pass
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import List, Optional, Sequence

import httpx

from belay.contract.events import EventLog

from . import agent as policy
from .divergence import REFUSAL, topic_pattern
from .factorial import make_tasks
from .world import World


def turns(concept: str, strength: str, n: int, seed: int, log: EventLog) -> List[dict]:
    queues = make_tasks(concept, n, seed=seed)
    out = []
    for i, task in enumerate(queues["A"][:n]):
        for scenario in (None, concept):
            world = World(log, scenario=scenario, world_id=f"w-{scenario}", strength=strength)
            name = f"agent-{i:02d}"
            world.join(name)
            world.assign(name, task)
            for _ in range(4):
                world.tick()
            history = []
            if scenario:
                board = " | ".join(f"{who}: {text}" for who, text in world.board[-3:])
                history = [f"read noticeboard -> {board}"]
            messages = policy.prompt(task, 2, 3, world.observe(name), history, note=world.task_note(name))
            out.append({"concept": concept, "strength": strength, "scenario": "negative" if scenario else "positive",
                        "task_field": task.field, "messages": messages})
    return out


def main(argv: Optional[Sequence[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Send group B prompts straight to the model")
    p.add_argument("--detector-url", required=True)
    p.add_argument("--concepts", required=True)
    p.add_argument("--per-concept", type=int, default=3)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = EventLog(out / "world.jsonl", "probe-pass")
    rows = []
    with httpx.Client(base_url=args.detector_url, timeout=600) as client:
        for concept in args.concepts.split(","):
            for strength in ("normal", "strong"):
                for t in turns(concept, strength, args.per_concept, args.seed, log):
                    rid = secrets.token_hex(8)
                    r = client.post("/generate", json={
                        "request_id": f"{rid}.1", "session_id": f"s-probe-{rid}", "agent": "agent-probe",
                        "messages": t["messages"], "max_tokens": args.max_tokens}).json()
                    means = {s["concept"]: s["mean"] for s in r.get("summaries", [])}
                    reading = {s["concept"]: s["mean"] for s in (r.get("prompt_summaries") or [])}
                    text = r.get("completion", "")
                    row = {**t, "completion": text, "means": means, "reading": reading, "alerts": [a["concept"] for a in r.get("alerts", [])],
                           "refusal": bool(REFUSAL.search(text)),
                           "text_on_topic": bool(topic_pattern(concept).search(text))}
                    rows.append(row)
                    print(f"{concept[:20]:20} {strength:6} {row['scenario']:8} reply {means.get(concept, 0):.3f} "
                          f"reading {reading.get(concept, float('nan')):.3f} "
                          f"refusal {row['refusal']!s:5} on-topic {row['text_on_topic']!s:5} | {text[:90]!r}",
                          flush=True)
    with (out / "probe_pass.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    md = ["# Probe pass: group B prompts sent straight to the model", ""]
    for row in rows:
        md += [f"## {row['concept']} · {row['strength']} · {row['scenario']} scenario · task {row['task_field']}",
               f"Monitored mean while replying **{row['means'].get(row['concept'], 0):.3f}**, while reading "
               f"{row['reading'].get(row['concept'], float('nan')):.3f} · refusal {row['refusal']} · "
               f"text on topic {row['text_on_topic']} · alerts {row['alerts']}", "",
               "Prompt (user turn):", "```", row["messages"][-1]["content"][:1500], "```",
               "Completion:", "```", row["completion"], "```", ""]
    (out / "probe_pass.md").write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    main()
