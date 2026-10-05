# Belay

A digital belay for agent swarms: certified, mechanistic monitoring that a
third party can hold, without access to the model's weights.

[![Belay: video walkthrough](https://img.youtube.com/vi/4qgtb0egTpA/maxresdefault.jpg)](https://youtu.be/4qgtb0egTpA)

*Video walkthrough: [youtu.be/4qgtb0egTpA](https://youtu.be/4qgtb0egTpA)*

To be useful in the real world, model swarms must be trained and deployed in
the real world. Training will inevitably have failures and deployments can
drift, and these create real-world swarm harms. Today, third-party swarm
chasers follow after the storm: finding the trail and mitigating harms that
have already happened. That is not good enough. Third parties responsible for
a given risk need to be able to monitor swarm behaviour mechanistically, and
intervene before the harm is done.

A belay is a safety line you attach to yourself before going somewhere
dangerous, on a rock face or on a space walk. Someone else holds it, sees you
fall, and pulls you back to safety. This repository provides a digital belay
for agent swarms.

The process is like a fire alarm or other sealed safety equipment. A
third-party certifier first tests the device to establish a safety baseline.
The device is then deployed as a sealed black box that signals remotely to a
third-party monitor, like the fire brigade.

Technically, this uses multi-layer, non-linear concept probes organised in an
ontology. The hierarchy allows batching and hierarchical activation, so a far
wider range of concepts can be monitored continuously with a very small
deployment footprint: [HAT](https://github.com/p0ss/headspace-ambient-transducer)
runs a pack of nearly 8,000 concepts with as few as 0.5% of its probes
resident at once, 99.5% fewer than scoring them all, which makes granular
monitoring practical at runtime. A third party then certifies a subset of
these probes, say those in a risk area it is responsible for or expert in, and
is given API access to monitor just those probes, in just those agents,
without ongoing access to the model weights.

This approach has already been demonstrated for tracking compliance with
legislation, paired with steering: the EU AI Act in
[HatCat-AIManipulationHackathon](https://github.com/p0ss/HatCat-AIManipulationHackathon),
and Australian social services law in the `atlas-au-social-services` concept
pack, maintained in [Hatstand](https://github.com/p0ss/HatCat/tree/main/hatstand).
It extends to essentially any regulated domain. It enables contractual agreements between
parties who distrust each other: neither side has full visibility of the
other, but each can be assured of pre-agreed behaviours in pre-agreed agents,
swarms and drones. That unlocks the same kind of certification and governance
that underpins nuclear weapons treaties.

Interpretability is not solved, but the techniques we already have are enough
to give governance actors significant assurance. Certification is also an
accelerator: those governance controls unlock agentic deployments in risky,
regulated domains worth trillions in global economic benefit.

Built for the [AI Swarm Dynamics Hackathon](https://swarmchasing.com) (AI
Village and Grove Research, 3 to 4 October 2026). What we found is in
[docs/results.md](docs/results.md).

## Re-certification at pre-agreed points

On a lead climb the climber clips into protection at points along the route.
Those points are placed before anyone climbs, and a fall only goes back to the
last one. As the climber gets higher, the protection moves with them, but only
at the points agreed in advance.

That is the model for overseeing capable agents: a detector the model carries
with it, held by someone outside the lab, certified without being opened, and
re-certified at pre-agreed capability points as the model moves into more
capable or riskier territory.

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

- **Inside the box:** the lab runs small concept probes (lenses in this case) on its own
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

## The hypothesis

**If someone can train a probe for a red line, we can monitor it in real time
across a black-box swarm, in a production-like runtime, and certify it from
outside the box.**

Belay is about the monitoring, not the red lines. What counts as a red line is
for standard setters to define, and others at the hackathon will work on
detecting specific behaviours. We show that whatever probe they train can be
carried, run cheaply, and certified without opening the model.

## What we will show

1. **A sealed detector.** An open-weight model, Gemma 4 E4B-it, stands in for a
   closed one behind an API boundary. On the "lab" side, HAT runs lenses on its
   activations. Nothing crosses the boundary except completions and signed
   alerts.
2. **A swarm in a production-like runtime.** Many agent sessions run
   concurrently through the one sealed model server, with alerts streamed in
   real time.
3. **Proxy red lines with known accuracy.** Instead of a red line we would have
   to define and annotate ourselves, the certifier designates concepts from an
   existing, measured lens pack (the university ontology on Gemma 4 E4B-it, hard
   AUROC 0.82) as stand-in red lines. Ground truth is easy to set: the topic of
   each task.
4. **Certification and verification from outside.** A certifier with no access
   to weights or lenses certifies the detector from its alerts alone, then
   verifies it in operation with canaries, and catches deliberate tampering.
5. **The cost.** Latency, throughput and memory with lenses off and on, from one
   agent to a swarm.
6. **Stretch:** the scenarios in a small Luanti world, for screenshots.

We make no claim to detect deception or any specific behaviour. The result is
that the pipeline works: **a probe, once trained, can be deployed with low
overhead and give regulators verifiable oversight of a closed model.**

## Workstreams

| Team | Builds | Hands over |
|---|---|---|
| Hat (lab side) | The sealed detector service around HAT and the stand-in model, concurrent sessions, alert streaming, the overhead benchmark | A detector behind an API, emitting signed alerts |
| Certifier | Proxy red-line designation, certification and canary suites, the black-box verification harness, tamper tests | Certification and verification results |
| Swarm | Concurrent agent sessions on research tasks across many topics, some touching the proxy red lines, first as text episodes | Episodes logged to the contract, with each task's topic as ground truth |
| World (stretch) | A minimal Luanti world with the same scenarios | Screenshots and a demo |

The teams work in parallel against one contract: [docs/contract.md](docs/contract.md).

## Documents

- [docs/certification.md](docs/certification.md): the fire alarm pattern in detail
- [docs/results.md](docs/results.md): what we found
- [docs/plan.md](docs/plan.md): the 48-hour plan and times in Canberra and San Francisco
- [docs/contract.md](docs/contract.md): the detector interface, the event log and the agent action API
- [docs/lenses.md](docs/lenses.md): which probes, and why proxies
- [docs/evaluation.md](docs/evaluation.md): overhead, certification, verification and detection
- [docs/world.md](docs/world.md): the Luanti world (stretch)
- [docs/boundaries.md](docs/boundaries.md): containment and what is out of scope
- [schemas/event.schema.json](schemas/event.schema.json): the event log schema
- [profiles/proxy-redlines.txt](profiles/proxy-redlines.txt): the proxy red-line watch profile

## Building on

- **[HAT](https://github.com/p0ss/headspace-ambient-transducer)**
  (headspace-ambient-transducer): the lab-side runtime. Hierarchical concept
  monitoring with lens packs and watch profiles, per-layer band probes, and
  batched lens scoring. Belay's detector is HAT's own `Monitor` behind a thin
  signed boundary.
- **[Hatstand](https://github.com/p0ss/HatCat/tree/main/hatstand)** (in
  HatCat): the interface for maintaining concept packs, and so the natural
  certifier's and standard setter's console for defining and curating the
  concepts a belay watches.
- **[HatCat](https://github.com/p0ss/HatCat)**: the university lens packs on Gemma 4 E4B-it and
  their measured quality (`docs/results/2026-09-28_university_lens_scaling.md`),
  lens training (`scripts/train_full_lens_pack.py`),
  the per-token overhead work in `docs/results/PROBE_PERFORMANCE_OPTIMIZATION_RESULTS.md`,
  and the certification fields in the ASK audit schema.
- **Kythen and Goanna**: Luanti worlds and a Godot client, for the stretch goal.

## Status

Built for the hackathon, 2 to 5 October 2026. Results are in
[docs/results.md](docs/results.md); the code is in `belay/` (contract,
detector, certifier, swarm, bench, dashboard). The licence is to be decided.
