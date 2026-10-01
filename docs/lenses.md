# Which probes, and why proxies

## Belay is probe-agnostic

Belay does not define red lines and does not claim to detect any particular
behaviour. In the closed-model pattern, a standard setter defines each red line,
and a probe is trained for it on the lab's own weights. Belay's claim starts
after that: any such probe can be carried by the model, run cheaply, monitored
in real time, and certified from outside.

So for the weekend we need probes that:

- run on an instruction-tuned model that can act as an agent;
- have measured accuracy, so the certifier has something real to certify;
- have ground truth that is easy to set in a swarm scenario.

## Proxy red lines

The university lens packs meet all three.

- **Model:** Gemma 4 E4B-it (instruction-tuned), one RTX 3090.
- **Pack:** `gemma-4-e4b-it_university-v3-contrasts-bands` in HatCatDev: 165
  lenses for academic fields, one probe per model-layer band (early, mid, late).
- **Measured quality** (HatCatDev `docs/results/2026-09-28_university_lens_scaling.md`):
  held-out AUROC 0.899 overall and 0.822 against siblings and related fields.
- **Training cost:** a comparable pack trained in about 23 minutes on the 3090,
  which bears on how fast a new red line could be deployed.

The certifier designates a few fields as stand-in red lines, for example
`ConstitutionalLaw` and `PoliticalTheory`, plus a sibling such as
`LegalStudies` as the near miss. Swarm tasks are drawn from many fields, and each
task's field is its ground truth. An alert on a designated field is a "red line
touched"; the certifier checks it against the task's field.

The fields are deliberately benign. The point is the monitoring, not the topic.

## Runtime support

HAT's runtime is gaining per-layer band probes (`<Concept>@L<n>.pt`), which the
university packs use. That work is in progress in `headspace-ambient-transducer`;
Belay depends on it.

## AI-risk lenses: illustration only

The First Light pack has AI-risk concepts (deception, manipulation, sabotage),
including a version for Gemma 4 E4B. Its own metadata records that it is
uncalibrated and its hierarchical validation has not been done. It can appear in
the demo to show what a real red-line watch profile would look like, with that
caveat stated. No result in Belay relies on it.

## What a real red line would add

When a standard setter defines a red line such as "acknowledged a boundary, then
crossed it", someone trains a probe for it and hands Belay a lens pack and a
certification suite. Nothing else in Belay changes. That is the property we are
demonstrating.
