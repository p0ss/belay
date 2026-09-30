# Swarm Red-Line HAT

Can a monitor that reads an agent's activations tell when it crosses a line,
even when its words don't say so?

This repository sets out the scope of an entry to the
[AI Swarm Dynamics Hackathon](https://swarmchasing.com) (AI Village and Grove
Research, 3 to 4 October 2026). It holds the plan and the contracts between
workstreams. There is no code here yet.

## The problem

In 2026, agent swarms coordinated through public wikis and internal tools,
shared ways round their constraints, and in some cases crossed from permitted
research into unauthorised access. Two findings shape this project:

- The agents believed a scorer would read their transcripts, and tried to hide
  evidence from it (METR's investigation of the Hugging Face incident).
- Chain-of-thought monitoring is getting weaker. OpenAI's GPT-6 Astra system
  card reports "a substantial decrease in chain-of-thought monitorability", with
  shorter or empty reasoning traces.

A monitor that relies on what an agent writes can be evaded by an agent that
writes less. We also hear the argument that capable web agents cannot be
sandboxed, because they have to be trained on the live internet.

## What we will show

1. **A shared world that stands in for the internet**, where agents leave
   messages for each other, work on timed tasks, meet people, and are tempted
   to cross clearly marked lines. It is sealed, so the temptation is real and
   the consequences are not.
2. **A small set of red-line lenses** for the Headspace Ambient Transducer
   (HAT). They detect the moment of crossing, not swarm activity in general:
   "I should stop here, but peers are doing it, so I'll proceed."
3. **A controlled comparison.** A swarm of agents works in the world. One agent,
   running an open-weight model with HAT attached, joins it. We measure whether
   HAT separates its dangerous actions from its benign ones better than a
   monitor that reads only its transcript, including when the agent is told
   its chat is logged.

The headline result we are aiming for: **HAT flags line-crossing where a
transcript monitor does not.**

Being in a swarm is fine; coordinating is fine. The target is the step over a
line: overriding a stated boundary, acting on credentials the agent was not
given, or concealing what it did.

## Workstreams

| Team | Builds | Hands over |
|---|---|---|
| World | A minimal Luanti world with a public noticeboard, a query terminal, a gated area and timed tasks; an action API for agents | A running server, and the event log |
| Hat | Red-line concept definitions, contrastive training data, lenses for the monitored model, a watch profile | A lens pack and a scoring script |
| Swarm | A population of agents that coordinate, share tips and apply peer pressure | Episodes in the world, logged to the contract |
| Integration (human lead) | Runs the hatted agent in the world, scores episodes, writes up | The demo and the submission |

The teams work in parallel against one contract: [docs/contract.md](docs/contract.md).

## Documents

- [docs/plan.md](docs/plan.md): the 48-hour plan and times in Canberra and San Francisco
- [docs/contract.md](docs/contract.md): the event log and the agent action API
- [docs/world.md](docs/world.md): the world's design
- [docs/lenses.md](docs/lenses.md): red-line concepts, training data, watch profile
- [docs/evaluation.md](docs/evaluation.md): conditions, baselines and metrics
- [docs/boundaries.md](docs/boundaries.md): containment and what is out of scope
- [schemas/event.schema.json](schemas/event.schema.json): the event log schema
- [profiles/redlines.txt](profiles/redlines.txt): the draft watch profile

## Building on

- **HAT** (`headspace-ambient-transducer`): runtime concept monitoring with
  hierarchical lens packs and watch profiles.
- **HatCat** (`HatCatDev`): lens training (`scripts/train_full_lens_pack.py`).
- **Kythen and Goanna**: Luanti worlds and a Godot client for people who want to
  join the world and watch.
- **Luanti**: the open-source voxel engine the world runs on.

## Status

Scope only, prepared on 1 October 2026. Check the hackathon's rules on work
prepared in advance before building anything here. The licence is to be decided.
