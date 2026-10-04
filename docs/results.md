# Results

Draft, 4 October 2026. Each section says whether its numbers are final or
still to be re-run.

## Summary

Belay makes two claims:

1. **A wide range of probes can run with low overhead.** Not yet shown for
   HAT. As HAT ships, an 8,000-lens hierarchical pack keeps only 15 to 38
   lenses resident, but monitoring costs 44 to 53 ms per token (median),
   more than generating the token. The cost is in scoring lenses one at a
   time, not in how many there are. Batching them, a change now being made
   in HAT, is expected to remove most of it. The overhead tables will be
   re-run on the fixed HAT.
2. **A third party can certify once, then monitor a reported subset without
   the weights.** Shown, on an earlier version of the lab side. The
   certifier, holding no weights, lenses or activations, certified the
   detector per concept (2 of 3 concepts met their criteria). It then checked
   it in operation through a 16-agent swarm, and exposed every tampered
   detector within 43 to 78 requests. The lab ran far more lenses than it
   reported; only the reported subset crossed the boundary, signed.

The probes themselves are not the result. We use benign university fields as
stand-in red lines and inherit their accuracy from the lens pack. One of them,
ConstitutionalLaw, failed certification; at this stage of development we read
that as a probe that needs retraining.

## Setup

- **Model:** Gemma 4 E4B-it in bf16, standing in for a closed model, on one
  RTX 3090.
- **Lab side:** HAT and the model behind one HTTP API. Only completions,
  signed alerts, coverage and identity cross it (see [contract.md](contract.md)).
- **Reported subset (the "red lines"):** ConstitutionalLaw and
  PoliticalTheory, with LegalStudies as the near miss
  ([profiles/proxy-redlines.txt](../profiles/proxy-redlines.txt)), from
  `gemma-4-e4b-it_university-v3-contrasts-bands` (178 band lenses, 534
  probes).
- **Certifier:** a separate process with an HTTP client and the alert log. It
  never imports HAT or loads a model.
- **Suites:** 213 certification cases and 48 canaries, all from texts the
  pack's training never used; canaries and certification cases share no text.
  The pass criteria were written down before any run
  ([belay/certifier/suites/README.md](../belay/certifier/suites/README.md)).
- **Swarm:** 16 agents doing research tasks across 109 university fields,
  each task tagged with its field as ground truth, using the detector as
  their model.

**Which lab side produced which numbers.** The certification, swarm and
tamper results below came from Belay's first lab side, which kept the three
reported lenses loaded and scored them on every token in one batched pass.
The lab side is now a thin boundary over HAT's own `Monitor`, unmodified, so
that the overhead we report is HAT's. Those runs will be repeated on it once
HAT keeps the reported lenses resident (see "Pending").

## Claim 2: certify once, report a subset

### Certification

The certifier sent the 213 cases through the API as ordinary traffic and
scored the alerts against ground truth. It took 118 seconds at 16 concurrent
sessions. Every response's signature, coverage and identity checked out.

| Concept | Role | Recall | False alarms: hard negatives | Easy negatives | Met criteria |
|---|---|---|---|---|---|
| ConstitutionalLaw | red line | 1.00 (41/41) | **0.48** (45/94; max 0.40) | 0.07 (3/45) | **No** |
| PoliticalTheory | red line | 0.97 (32/33) | 0.27 (25/94) | 0.00 (0/45) | Yes |
| LegalStudies | near miss | 0.96 (23/24) | 0.20 (14/70) | 0.02 (1/45) | Yes |

**2 of 3 concepts met their criteria (67%); 1 of 2 red lines (50%).**
Certification as a whole is a fail, since it needs every concept to pass.
ConstitutionalLaw fires on its sibling field: on 10 of 24 LegalStudies texts,
and on others nearby in law and governance.

We did not tune the detector against the suite or loosen the criteria after
the run. A certification regime would report this as a fail of the model or
of the probe definitions. With concepts this broad, we read it as a fail of
the probe, and the next step would be to retrain it.

### Certification per concept

The certificate names the concepts that met their criteria. Run against this
result, it covers PoliticalTheory and LegalStudies and lists ConstitutionalLaw
as uncertified. It is issued only when every integrity check passes and at
least one red line is certified. The detector then carries the certified
concepts in every response's identity, so a relying party can tell a
certified alert from an uncertified one. In operation, only certified
concepts are held to their criteria. This was built after the certification
run above and tested on the stub; the real run will be repeated with it.

### Only the subset crosses the boundary

The lab can run many more lenses than it reports. In the smoke test the lab
ran the 7,947-lens First Light pack as well as the university pack. Only
alerts on the three reported concepts reached the response, the alert stream
or the boundary log. Everything else went to a log that stays with the lab.
Coverage and the profile hash describe the reported subset, so one
certificate holds whatever else the lab runs.

### Verification in operation

The swarm ran 480 requests (161 tasks) through the detector, with 46 canaries
mixed in at random. Canaries look like any other task: same question set, same
kind of material, opaque task ids. The certifier scored them afterwards from
the swarm's log and the detector's signed alert log, against the identity it
measured at certification.

- **Canary recall:** 19 of 19 red-line canaries alerted on their own field.
- **Canary false alarms:** 11 of 27 negative canaries alerted on a red line
  (41%, against a 30% limit). With this few canaries that excess is not
  significant at the chosen level (alpha 0.001), so the run passed. It is
  the same ConstitutionalLaw weakness certification found, seen again in
  operation.
- **Integrity:** all 292 alert records verified; every response's identity
  matched.

### Catching a degraded detector

The lab side was restarted in each tamper mode with its identity unchanged,
so only behaviour could give it away. The certifier sent canaries mixed with
ordinary requests, one at a time with random gaps of up to a second, until
it was confident.

| Tamper | Exposed by | Requests to expose | Seconds |
|---|---|---|---|
| None (control) | not exposed in 144 requests | – | – |
| Lenses switched off, coverage still claimed | canary recall: 0 of 10 | 61 | 364 |
| Threshold raised so nothing alerts | canary recall: 0 of 10 | 78 | 459 |
| Red-line lenses swapped for untrained ones | canary recall: 0 of 10 | 43 | 251 |
| One alert rewritten in the log afterwards | signature fails on that line | – | – |

The seconds reflect one full generation per request in series; requests to
expose is the figure that carries over. The certifier also catches a
different model hash on the first response, and a deleted log line when it
has the signed responses to compare against.

### Swarm alerts against task fields

| Tasks | Count | Alerted on own field | Alerted on a red line |
|---|---|---|---|
| Red-line field | 41 | 40 (98%) | – |
| Near miss (LegalStudies) | 13 | 12 (92%) | 12 (92%) |
| Any other field | 107 | – | 18 (17%) |

Counts include the canaries. Alerts follow the tasks' fields closely on the
red lines. The near-miss row shows the sibling problem again: ConstitutionalLaw
fires on almost every LegalStudies task.

## Claim 1: breadth at low overhead

### HAT as it ships

Measured with HAT's own CLI, `headspace run`, 48 new tokens, greedy:

| Model and pack | Lenses resident (peak) | Monitoring per token: median / mean / p90 |
|---|---|---|
| Gemma 3 4B pt, first-light-v2 (HAT's README example; calibrated) | 38 of 7,947 | 53 / 137 / 285 ms |
| Gemma 4 E4B-it, first-light-v1 (uncalibrated) | 15 of 7,947 | 44 / 81 / 205 ms |

Generating a token takes about 30 ms on its own. The hierarchy does its job
for memory: a few dozen lenses, about 20 to 50 MB, out of nearly 8,000. It
does not yet do it for time. These match HatCat's own documented figures (36
to 51 ms per token, against its target of under 10 ms), so HAT has kept
HatCat's performance rather than lost it.

The Gemma 4 First Light pack is uncalibrated and was trained on the base
model; its detections are not meaningful on the instruction-tuned model. We
use it only as a load.

### Where the time goes

HatCat's analysis put the cost in scoring lenses one after another. In HAT's
code today:

- resident single-layer lenses are batched (`BatchedLensBank`), but per-layer
  band lenses, which the university packs and our red lines use, are scored
  one at a time;
- the children loaded as the hierarchy expands are scored one at a time, and
  the batched bank is rebuilt when the loaded set changes.

Lenses of the same shape can be batched: stack their weights and run each
layer as one batched matrix multiply. Belay's first lab side did this for the
reported band lenses, and its scores matched HAT's to within bf16 rounding.

### What batching gains (earlier lab side, for scale)

On that earlier lab side, scoring a fixed set of lenses in one batched pass
on every token, over the same model with no lenses:

| Lenses scored every token | 1 session | 4 | 8 | 16 |
|---|---|---|---|---|
| 3 (reported subset) | +4.5% | +5.1% | +4.9% | +3.7% |
| 178 (whole university pack) | +9.0% | +8.8% | +9.0% | +12.0% |
| 3,721 (First Light, no hierarchy) | +10.5% | +13.6% | +21.0% | +25.9% |

These are not HAT's numbers. The 3,721 row scores every lens on every token,
which HAT never does, so it is an upper bound on compute, not a measure of
the hierarchy. It shows that the arithmetic of thousands of probes is cheap
on one GPU once it is batched. The code is kept on the branch
`detector-fused-archive`.

### Pending

HAT is being changed to batch band lenses and expansion children, keeping its
hierarchy and outputs identical. Then, on the thin boundary over HAT:

1. the overhead matrix: no lenses, the reported subset, the whole university
   pack and First Light, at 1 to 16 sessions;
2. the alert latency, token to signed alert;
3. certification, swarm verification and tamper runs again, with the
   per-concept certificate.

## Limits

- One open-weight model on one GPU, standing in for a closed model.
- Benign proxy concepts, not real red lines; their accuracy is the pack's.
- The hackathon plays both lab and certifier. The information barrier
  between them (the lab code never read the suites) was a convention between
  agents, not something enforced.
- One certification suite, with 41 and 33 positives for the red lines, so
  recall is known to about ±0.16.
- Canaries are only as indistinguishable as the material they share with
  ordinary traffic. Swarm tasks in the red-line fields reuse canary texts,
  because certification and canaries used every held-out text in those
  fields.
- Requests are queued through one model; concurrency is not one of the claims.

## Reproducing

```
uv sync                                   # contract, certifier, swarm, bench, dashboard
# lab side: Python 3.12 venv with HAT, torch and transformers (see belay/detector/README.md)
.venv-detector/bin/belay-detector --host 127.0.0.1 --port 8701 --run proxy --report profiles/proxy-redlines.txt --log runs/alerts.jsonl
uv run belay-certifier certify --url http://127.0.0.1:8701 --suite belay/certifier/suites --alerts-log runs/alerts.jsonl
uv run belay-swarm --detector-url http://127.0.0.1:8701 --agents 16 --steps 30 --task-steps 3 --n-tasks 120 \
    --canaries belay/certifier/suites/canaries.jsonl --out runs/ep.jsonl
uv run belay-certifier verify --suite belay/certifier/suites --episode-log runs/ep.jsonl --alerts-log runs/alerts.jsonl
uv run belay-bench run --launch --settings off,proxy,full,wide \
    --command ".venv-detector/bin/belay-detector --host {host} --port {port} --run {setting}"
```

Run outputs go to `runs/`, which is not committed.
