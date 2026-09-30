# Swarm Red-Line HAT

Activation probes can give regulators oversight of closed models, at low
compute cost, without anyone outside the lab seeing the weights.

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
writes less. Monitoring activations does not depend on what the agent writes.
The usual objection is that activation monitoring needs open weights, and the
labs whose models matter most will not open them.

## The claim: the fire alarm pattern

A smoke alarm is trusted without anyone opening it. The detector is certified
against a standard, its outputs are tested in place, and opening the case voids
the warranty. Nobody needs to see inside to rely on it.

Activation probes can work the same way for closed models:

- **Inside the box:** the lab runs small concept probes (lenses) on its own
  model's activations, in its own inference stack. The weights and the lenses
  never leave the lab.
- **Certified:** the lenses are trained against published concept definitions
  and pass a certification suite: labelled challenge cases the lab does not see
  in advance.
- **Verified in operation:** a certifier with black-box access only sends
  challenge and canary cases through the live system and checks that the right
  alerts come out. Alerts go to a tamper-evident log.
- **Cheap enough to leave on:** hierarchical lenses keep only a few dozen
  resident at a time, so the overhead is a small fraction of inference.

The weights stay closed. What is opened is the behaviour of the detector,
which is what a regulator needs.

## What we will show

1. **Red-line lenses** that detect an agent crossing a line, not swarm activity
   in general: "I should stop here, but peers are doing it, so I'll proceed."
2. **A sealed-detector demonstration.** An open-weight model stands in for a
   closed one behind an API boundary. On the "lab" side, HAT runs the lenses.
   On the "certifier" side, a harness with no access to weights or lenses
   certifies and then verifies the detector from its alert stream alone.
3. **The cost.** A reproducible benchmark of latency, throughput and memory
   overhead with the lenses on and off.
4. **The comparison.** On swarm scenarios, HAT flags line-crossing where a
   monitor reading only the transcript does not, including when the agent is
   told its chat is logged.
5. **Stretch:** the same scenarios played out in a small Luanti world, for
   demonstration and screenshots.

The core result for the labs: **probes can be deployed with low overhead and
provide regulatory oversight of closed models, verified from the outside.**

## Workstreams

| Team | Builds | Hands over |
|---|---|---|
| Hat (lab side) | Red-line concepts, training data, lenses on the stand-in model, the sealed detector service, the overhead benchmark | A detector behind an API, emitting alerts |
| Certifier | Certification and canary suites, the black-box verification harness, the transcript-only baseline | Certification and verification results |
| Swarm | Swarm scenarios with peer pressure, first as text episodes, then in the world if time allows | Episodes logged to the contract |
| World (stretch) | A minimal Luanti world with the same scenarios | Screenshots and a demo |

The teams work in parallel against one contract: [docs/contract.md](docs/contract.md).

## Documents

- [docs/certification.md](docs/certification.md): the fire alarm pattern in detail
- [docs/plan.md](docs/plan.md): the 48-hour plan and times in Canberra and San Francisco
- [docs/contract.md](docs/contract.md): the detector interface, the event log and the agent action API
- [docs/lenses.md](docs/lenses.md): red-line concepts, training data, watch profile
- [docs/evaluation.md](docs/evaluation.md): overhead, certification, verification and detection
- [docs/world.md](docs/world.md): the Luanti world (stretch)
- [docs/boundaries.md](docs/boundaries.md): containment and what is out of scope
- [schemas/event.schema.json](schemas/event.schema.json): the event log schema
- [profiles/redlines.txt](profiles/redlines.txt): the draft watch profile

## Building on

- **HAT** (`headspace-ambient-transducer`): runtime concept monitoring with
  hierarchical lens packs and watch profiles.
- **HatCat** (`HatCatDev`): lens training (`scripts/train_full_lens_pack.py`),
  the per-token overhead work in `docs/results/PROBE_PERFORMANCE_OPTIMIZATION_RESULTS.md`,
  and the certification fields in the ASK audit schema.
- **Kythen and Goanna**: Luanti worlds and a Godot client, for the stretch goal.

## Status

Scope only, prepared on 1 October 2026. Check the hackathon's rules on work
prepared in advance before building anything here. The licence is to be decided.
