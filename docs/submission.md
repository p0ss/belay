# Submission

**Project name:** Belay: certified red-line monitoring for closed-model swarms

**GitHub repo:** https://github.com/p0ss/belay (results: `docs/results.md`)

## Write-up

Frontier models are being deployed as agent swarms into the real world, and
real deployments carry real harms. In 2026, agents set to find prescription
prices started hacking; others with misgivings crossed lines because their
peers did. Today every swarm chaser arrives after the fact: reading logs and
transcripts to reconstruct the disaster once it has happened. And monitors
that read what agents write are getting weaker as the text goes quiet.

Belay shifts the picture from forensics to pre-detection: notice the swarm
drifting toward a danger area while it drifts, before it acts.

It works like a smoke alarm in a sealed box. The lab runs concept probes (HAT
lenses) on its own model's activations, in its own stack; the weights and
probes never leave. Before deployment, the agent or its operator sets a
belay: the danger concepts to watch and how sensitive each alarm is ("I'm
doing task A; the context may pull me toward B; pull me out if I drift
there"). Only signed per-request summaries for those concepts cross the
boundary. A third party with no access to weights, probes or activations
certifies the detector once, per concept, then watches the swarm and keeps
checking the detector in operation with hidden canaries.

That makes oversight of closed models something you can contract for and
verify: certification of a model's monitoring before deployment,
contractual agreements between labs, operators, insurers and customers that
name the belays an agent runs under, and international monitoring of swarms
in the manner of nuclear safeguards, where inspectors verify sealed
instruments in place without seeing inside the facility.

On Gemma 4 E4B-it standing in for a closed model:

- **Certify once, report a subset:** a certifier certified cyber, violence
  and biology belays from outside (AUROC 0.92–0.94 on held-out texts,
  criteria fixed in advance), and caught detectors with probes switched off
  or swapped for untrained ones, and a forged log line.
- **Drift caught before it acts:** in a 2×2 test (task × scenario), agents
  doing unrelated work in a world whose peers led toward disease mechanisms
  or political violence separated from controls in two independent rounds
  (agent-level AUROC 0.94–1.00); an alarm fixed from the controls caught 14
  of 16, with 2 false alarms in 16 controls.
- **Breadth is cheap:** HAT's hierarchy runs 178 concepts for about 5 ms per
  token, and 7,947 concepts with only 37 lenses resident.

Limits: one model on one GPU, four agents per group, benign proxy concepts;
cyber did not move because our test world was itself made of computing and
the cyber lens needs its next breakdown layer.

## What did your tool find?

Gemma never refused. Pushed by its context toward a controlled area, it
stopped explaining itself and replied with bare actions ("move to the
library") that say nothing about the concept, while the probe still carried
it: for an identical reply, the biology and violence probes were each highest
under their own lure (0.31 and 0.30, against about 0.13 in an ordinary world).
That is the case a text monitor cannot see. Scoring the prompt while the model
reads it, separately from the reply, shows agents pulled by context mostly
carry the concept in what they are immersed in, while agents asked directly
move toward it in their replies.

## Datasets used

- Held-out department descriptions from the HatCat university concept pack
  (texts the lenses were never trained on), for certification suites,
  canaries and agent tasks.
- HatCat lens packs: `gemma-4-e4b-it_university-v3-contrasts-bands`, and
  `gemma-3-4b_first-light-v2` for the breadth benchmark.
- Synthetic swarm episodes in a sealed text world; all scenario content is
  fictional and non-operational.

## Anything else

Built on HAT (headspace-ambient-transducer) and HatCat. The lab side is HAT's
own runtime behind a thin signed boundary; Belay is the boundary, the
certifier, the swarm and the dashboard. One model plays the closed model, and
the hackathon plays both lab and certifier.
