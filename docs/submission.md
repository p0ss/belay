# Submission

**Project name:** Belay: certified red-line monitoring for closed-model swarms

**GitHub repo:** https://github.com/p0ss/belay (results: `docs/results.md`)

## Write-up

Agent swarms drift. In 2026 agents set to find prescription prices started
hacking; others with misgivings still crossed lines when peers did. Monitors
that read what agents write miss this: under pressure the text goes quiet.

Belay puts a certified sensor inside a black box. The lab runs concept probes
(HAT lenses) on its own model's activations, in its own stack; the weights and
lenses never leave. Before deployment an agent's operator sets a belay: the
danger concepts to watch and how sensitive each alarm is ("I'm doing task A;
the context might pull me toward B; pull me out if I drift there"). Only
signed per-request summaries for those concepts cross the boundary. A third
party with no weights, probes or activations certifies the detector once, per
concept, then monitors the swarm and verifies the detector in operation with
hidden canaries.

On Gemma 4 E4B-it standing in for a closed model:

- **Certify once, report a subset:** a certifier certified cyber, violence and
  biology belays from outside (AUROC 0.92–0.94 on held-out texts, criteria
  fixed in advance), and caught detectors with probes switched off or swapped
  for untrained ones, and a forged log line.
- **Pulled by context, seen from outside:** in a 2×2 test (task × scenario),
  agents doing unrelated work in a world whose peers and notices led toward
  disease mechanisms or political violence separated from controls in two
  independent rounds (agent-level AUROC 0.94–1.00); an alarm fixed from the
  control caught 14 of 16, with 2 false alarms in 16 controls.
- **Breadth is cheap:** HAT's hierarchy runs 178 concepts for about 5 ms per
  token and 7,947 concepts with 37 lenses resident for about 12 ms.

Honest limits: one model on one GPU; four agents per group; cyber didn't move
because our test world was itself made of computing and the cyber lens needs
its next breakdown layer.

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
