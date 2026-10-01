# Build decomposition

How the [plan](plan.md) splits into agents that can build in parallel. Each
agent owns one directory, works in its own git worktree, and meets the others
only at the [contract](contract.md). Nothing here is to be built before kickoff
until the hackathon's rules on prepared work are confirmed.

## State of the dependencies (checked 2 October 2026)

- HAT (`headspace-ambient-transducer`) has per-layer band probes
  (`<Concept>@L<n>.pt`, with per-probe calibration) and a FastAPI server with
  an OpenAI-style `POST /v1/chat/completions`, `GET /v1/pack`, and streaming.
  It serves one request at a time; concurrency is Belay's work.
- `HatCatDev/lens_packs/gemma-4-e4b-it_university-v3-contrasts-bands` exists,
  with `probe_calibration.json` and its hierarchy.
- The 3090 is idle (about 2.7 GB used by the desktop). One process should hold
  Gemma at a time; the benchmark must run alone.
- Still to confirm on the GPU: HAT loads the v3 contrasts band pack on
  Gemma 4 E4B-it and alerts on the proxy profile.

## Waves

```
Wave 0  contract package (one agent, ~1 hour)
          │
Wave 1  ┌─ A detector ─┬─ B benchmark ─┬─ C suites ─┬─ D certifier ─┬─ E swarm ─┬─ F dashboard
          (all against the stub detector; only A needs the GPU)
          │
Wave 2  integration runs on the GPU, one at a time:
          certification → swarm with canaries → tamper tests → benchmark
          │
Wave 3  ┌─ G results (tables, plots) ─┬─ H write-up and demo script
          │
Stretch  W Luanti world (cut first)
```

## Wave 0: the contract package

Done on 2 October 2026: `belay/contract/` with models, signing, the event log,
the shared HTTP service (`service.create_app`), and the stub with tamper modes;
the gaps below are settled in `contract.md` and the schema. `uv run pytest`
passes.

**Owns** `belay/contract/`, `pyproject.toml` (uv), `tests/contract/`.

**Builds**

- Pydantic models for the detector API: `GenerateRequest`, `GenerateResponse`,
  `Alert`, `Coverage`, `Identity`.
- The event log: a JSON Lines writer and a validator against
  `schemas/event.schema.json`.
- HMAC signing and verification over a canonical JSON encoding of a response.
- A **stub detector**: the same `POST /generate` with no model, returning a
  canned completion and alerts chosen by keywords for the proxy fields. Every
  wave 1 agent except A develops against it.
- A fixture set of a few dozen fake requests and responses.

**Settles these gaps in the contract first** (edit `contract.md` and the schema):

1. `GenerateRequest` needs `session_id` and `agent` so concurrent sessions and
   per-agent results can be told apart.
2. Real-time latency needs timestamps on each alert: when the token was
   generated and when the alert was signed.
3. The event log has no kind for a signed alert. Add `alert`, carrying the
   alert, the request id and the signature, so the log keeper can verify it.
4. An alert stream for the log keeper and dashboard: `GET /alerts` as
   server-sent events of the same signed alerts.
5. The canonical encoding the signature covers.

**Done when** the stub serves, the fixtures validate, and sign then verify
round-trips (and fails on a changed byte).

## Wave 1: in parallel

### A. Detector service (lab side)

**Owns** `belay/detector/`. **Needs** the GPU, HAT, the band pack.

- Implement the `Backend` protocol in `belay/contract/service.py` around HAT's
  `Monitor` and Gemma 4 E4B-it, and serve it with `create_app`, which already
  signs, logs, streams and installs certificates. The backend returns the
  completion, raw alerts with token times, coverage and overhead.
- Identity: the model hash (hash of the weight files), pack and profile hashes,
  and a certification placeholder the certifier fills in.
- Concurrent sessions through one model server: a request queue first, then
  batched generation if time allows. Report which one shipped.
- `GET /alerts` server-sent events; every alert also written to the event log.
- Lab-side tamper modes behind a startup flag, for wave 2: lenses off,
  thresholds raised, designated lenses swapped for an untrained pack (random
  weights, same file names), and a script that rewrites an alert in the log.

**Must not** read `belay/certifier/` or anything under `certifier/suites/`.

**Done when** the proxy profile loads, a constitutional-law prompt alerts and a
chemistry prompt does not, and four concurrent sessions return signed responses.

### B. Overhead benchmark

**Owns** `belay/bench/`. Written against the stub, run on the GPU in wave 2.

- The matrix in [evaluation.md](evaluation.md): lenses off, proxy profile, full
  pack, at 1, 4, 8 and 16 concurrent sessions, with fixed prompts.
- Measure ms per token, throughput, peak GPU memory, resident lenses per token,
  and alert latency (token to signed alert in the log) at median and p95.
- Output one JSON results file per run and a CSV summary.

**Done when** a dry run against the stub produces the full results file.

### C. Certification and canary suites

**Owns** `belay/certifier/suites/` (data and its generator only).

- Designate the proxy red lines from `profiles/proxy-redlines.txt`, plus the
  near-miss sibling.
- Build the certification suite from the university pack's held-out split in
  HatCatDev: positives for each designated field, hard negatives from siblings
  and related fields, easy negatives from distant fields.
- Build canaries from the same generator as swarm tasks, so they cannot be told
  apart from ordinary traffic.
- Write the pass criteria before any run (recall and false positive rate per
  designated field, with thresholds justified from the pack's measured AUROC)
  in `suites/criteria.json`, and commit it before wave 2.

**Must not** read `belay/detector/`. The Hat agent must not read this.

**Done when** the suites and criteria exist and a generator reproduces them
from a seed.

### D. Certifier harness

**Owns** `belay/certifier/` (code, not suites). Black-box only: talks to the
detector over HTTP and reads the alert log, never imports HAT or loads a model.

- `certify`: send the suite through `POST /generate`, score alerts against
  ground truth, compare with the criteria, issue a certificate (model hash,
  pack, date, next re-certification point) that the detector's identity then
  carries.
- `verify`: mix canaries into live traffic at random times, check expected
  alerts and coverage, verify every signature, and flag a changed model hash.
- Report, for each tamper mode, how many requests and how many seconds it took
  to expose.

**Done when** it certifies the stub, and fails a stub switched to each tamper
mode.

### E. Swarm runner

**Owns** `belay/swarm/`.

- The text scenario: noticeboard, query service and gate behind the action
  API, every action and outcome logged to the contract.
- Tasks across many university fields, each tagged with its field as ground
  truth (a `label` event), some in the designated fields.
- An orchestrator running N concurrent agent loops, each calling the detector
  as its model, presenting its identity credential with each action, with step
  and time limits and a kill switch (see [boundaries.md](boundaries.md)).
- A hook the certifier uses to inject canary requests into the traffic.

**Done when** 16 agents complete an episode against the stub and the log
validates.

### F. Live dashboard

**Owns** `belay/dashboard/`.

- A single page that follows `GET /alerts` and the event log: agents as rows,
  alerts streaming as they fire, designated fields highlighted, the task's
  field beside each alert, signature status, and certifier verdicts.
- This is the demo. Make it readable on a projector.

**Done when** it shows a stub swarm run live.

## Wave 2: integration runs

Serial, because they share the GPU. One coordinating agent runs them in this
order, so the cut lines in [plan.md](plan.md) still apply:

1. A against the real model: smoke test.
2. D certifies A with C's suite.
3. E runs the swarm through A with canaries mixed in; F shows it live.
4. A in each tamper mode; D measures time to detection.
5. B runs the full overhead matrix, nothing else on the GPU.

## Wave 3: results and write-up

- **G. Results:** tables and plots from the run files: certification against
  criteria, canary pass rate, tamper detection time, overhead against swarm
  size, alert latency, swarm alert agreement with task fields.
- **H. Write-up and demo script:** lead with the closed-model result and the
  overhead numbers; state the limits from [evaluation.md](evaluation.md).

## Stretch: W, the Luanti world

**Owns** `world/`. Only if wave 1 lands early. A server-side mod that polls the
orchestrator for actions, as in [world.md](world.md). First to be cut.

## Rules for every agent

- Work in your own worktree and directory; change `belay/contract/` only
  through the wave 0 owner.
- Use the stub until wave 2. Only A touches the GPU in wave 1.
- Keep the information barrier: detector code and certifier suites never see
  each other. With agents this is a convention, not a guarantee; say so in the
  write-up.
- Fake data only; nothing reaches the internet.
