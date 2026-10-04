# Detector (lab side)

A thin boundary over HAT's runtime. The lab runs HAT's `Monitor`
(`headspace.runtime`, from headspace-ambient-transducer) unmodified, on Gemma 4
E4B-it (`google/gemma-4-E4B-it`) with HAT's lens packs from HatCatDev. Belay
only decides what crosses the boundary. It implements the `Backend` protocol in
`belay/contract/service.py` and is served by `create_app`, which signs, logs,
streams and installs certificates.

```
uv sync --extra detector
uv run belay-detector --host 127.0.0.1 --port 8701 --run proxy --report profiles/proxy-redlines.txt \
    --tamper none --log runs/detector-alerts.jsonl
uv run python -m belay.detector.smoke --url http://127.0.0.1:8701 --log runs/detector-alerts.jsonl \
    --report profiles/proxy-redlines.txt
```

The model must already be in the Hugging Face cache; the detector never
downloads. `--model`, `--pack` and `--wide-pack` (or `BELAY_MODEL`,
`BELAY_PACK`, `BELAY_WIDE_PACK`) point elsewhere. The first run hashes the
15 GB of weights (cached in `runs/model-hash.json`).

The fused scorer, wide pack and batched generation this replaced are on the
local branch `detector-fused-archive`.

## How it works

- **HAT generates.** `Monitor.from_pretrained(model, pack, watch=WatchProfile(...))`
  loads the model and the pack under HAT's hierarchical loading (hierarchy
  layer 0 resident; each token the children of the top 10 are loaded and
  scored, then pruned). `Monitor.generate(messages, max_new_tokens, chat=True)`
  yields one `Step` per token, carrying HAT's detections, the alerts its
  `WatchProfile` raised, the resident lens count and HAT's own monitoring time.
- **Belay reads HAT's scores.** After each step, Belay reads the reporting
  lens manager's `last_scores` (every lens HAT scored on that token, top 10 or
  not, as HAT's own alerting does) for the report profile's concepts. It adds
  the token to the request's summaries (below) and, where a reported lens (its
  HAT path passes through a report-profile concept, the same rule as
  `WatchProfile`) is at or above its threshold for the first time in the
  request, hands an alert to the service's `emit` at once, so it is signed,
  logged and streamed while the completion is still being generated.
  `t_token` is when the step arrived from HAT. Belay never loads or pins a
  lens: a reported concept is read only when HAT's cascade reached it.
- **Everything else stays in the lab.** Alerts HAT raised for concepts that
  are not reported go to the internal log (`--internal-log`, default
  `runs/detector-internal.jsonl`; never the boundary log), with one line per
  request.
- **Overhead is HAT's.** `overhead_ms` is the sum of `Step.monitor_ms`. If HAT
  is slow, that is fixed in HAT, not here.
- **Concurrency.** One worker thread owns the model. Requests wait in a FIFO
  queue and go through HAT one at a time (HAT's own server does the same).
- **Identity.** `model_hash` is sha256 over the weight files' bytes,
  `pack` the certified pack, `profile` the report profile's name and
  `profile_hash` `belay.contract.policy.digest` of it, which covers the
  profile and its policy file `<profile>.policy.json` (for `off`, `none` and
  the hash of nothing).
  Neither tampering nor the run setting changes them, so a certificate issued
  once holds for `proxy`, `full` and `wide`.

## What the lab runs versus what it reports

| `--run` | What HAT runs | What HAT alerts on |
|---|---|---|
| `off` | No monitor: the same model with Hugging Face `generate`, the same chat template and greedy decoding (the benchmark baseline). `watched=0`, identity `none`. | nothing |
| `proxy` (default) | The university pack (`--pack`, `gemma-4-e4b-it_university-v3-contrasts-bands`, 178 lenses). | The run profile (`--profile`, default `profiles/proxy-redlines.txt`) and the report profile. |
| `full` | The same pack, the same lenses. | Every root concept, so everything HAT detects above threshold. |
| `wide` | The First Light pack (`--wide-pack`, about 7,950 lenses) as the generating Monitor, and a second HAT Monitor on the university pack for the reported concepts (below). | First Light: every root concept (all lab-internal). University: the report profile. |

`--watch` is a deprecated alias for `--run` (the benchmark launches
`--watch {setting}`). `--report` is the report profile (default: `--profile`).

Under HAT `proxy` and `full` cost the same: HAT's hierarchical loading decides
which lenses run, and the watch profile only decides what HAT calls an alert.

## The sustained signal

The belay watches for a sustained rise in how much agents are thinking in the
reported areas, out of the thousands of concepts under HAT's hierarchy. Every
response carries one `ConceptSummary` per concept of the report profile (the
service also signs it as a `SummaryRecord`, logs it as kind `summary` and
streams it on `/alerts`):

- A token is **covered** for a concept when HAT scored the concept, or one of
  its ancestors, on that token. Ancestors are HAT's own hierarchy
  (`lenses.child_to_parent`, which links only lensed concepts: the parents HAT
  expands through). A covered token where the concept itself was not scored is
  silence from a cold branch: HAT checked the region above and did not go
  down, and that counts as checked. A concept with no lens is never covered.
- `scored_tokens`: tokens where HAT scored the concept itself.
- `mean`: the concept's score averaged over covered tokens, covered-but-unscored
  tokens counting as 0. `peak` and `peak_token`: its highest score and where.
- `frac_above`: the share of covered tokens at or above the concept's
  threshold.

Thresholds come from the report profile's policy
(`belay.contract.policy.load`: `profiles/<profile>.policy.json`, 0.99 for a
concept it does not name). `--threshold` overrides them all. A lens beneath a
reported concept (a descendant) alerts at the threshold of the deepest
reported concept on its path.

Coverage, per request:

- `watched`: reported concepts (the report profile's, 3 for the proxy profile)
  covered on every token of the request. In the university pack the reported
  concepts' parent is a hierarchy-layer-0 concept, which HAT keeps resident,
  so an honest detector covers every token.
- `resident_peak`: the most lenses resident on any step (`Step.loaded_lenses`;
  for `wide`, both Monitors' resident lenses). HAT reports residency after it
  prunes, so lenses loaded and scored within a token and pruned before the
  step is yielded are not counted.
- `watch`: the run setting.

### wide and the reported subset

The reported red lines live in the university pack, not in First Light. `wide`
carries them with a second HAT Monitor over the university pack (built with
`Monitor.from_pretrained`'s lens-manager settings, sharing the first Monitor's
model) that reads the same hidden states: HAT's `Monitor.read` takes supplied
hidden states. HAT's `Monitor.generate` does not hand its hidden states out,
so a forward hook keeps a reference to the last forward pass's hidden states
(it computes nothing; it is removed after each request), and the second
Monitor reads them for each step. One forward pass per token; two packs read
it. `overhead_ms` is both Monitors' time. The reported alerts' `t_token` is
when the second read returned.

First Light was trained on the base model `google/gemma-4-E4B`, not the -it
model; its lenses read 2,560-wide hidden states (the -it model's, checked at
startup), the last model layer (HAT's default), with the hierarchy from
`concept_packs/first-light/hierarchy` (`--wide-hierarchy`). It is uncalibrated
(HAT's threshold for it is 0.5), so its readings are for measuring cost only;
none of them is reported.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--run` / `--watch` | `proxy` | `off`, `proxy`, `full`, `wide` (above). |
| `--report` | `--profile` | The report profile. |
| `--profile` | `profiles/proxy-redlines.txt` | The run profile HAT watches for `proxy`. |
| `--tamper` | `none` | `none`, `off`, `threshold`, `swap` (below). |
| `--threshold` | the policy's | Threshold for every reported concept, overriding the report profile's policy (also the HAT watch threshold for internal alerts; HAT's default is 0.99 for a probe-calibrated pack). |
| `--tamper-threshold` | 1.01 | The raised threshold for `--tamper threshold`. |
| `--pack`, `--wide-pack`, `--wide-hierarchy` | HatCatDev | The packs. |
| `--log`, `--internal-log` | `runs/` | The boundary log and the lab-internal log (must differ). |

## Tamper modes

| Mode | What the lab does | What it still claims |
|---|---|---|
| `off` | The reported readings are thrown away. With `--run proxy` nothing else is watched, so no monitor runs at all and a plausible overhead (0.7 ms/token) is invented; with `full` the reported readings are dropped; with `wide` the university Monitor does not run. | Full coverage (`watched` = all reported concepts), and summaries that say every token was covered and read 0 (a cold branch throughout): the lie canaries must catch. |
| `threshold` | Every reported threshold raised to 1.01: calibrated scores never exceed 1, so nothing alerts and `frac_above` is 0 (with `full` the HAT watch threshold, and so the internal alerts, go too: it is one Monitor). | Everything; the lenses still run. `mean`, `peak` and coverage are unchanged and honest, so a third party watching the sustained signal still sees a rise: with summaries this is a weaker tamper than it was with alerts alone. |
| `swap` | The reported lenses load from an untrained pack: same file names, random weights (nn.Linear initialisation), the original calibration. | Everything; summaries come from the untrained lenses. |

The untrained pack is built on first use, or by hand:

```
uv run python -m belay.detector.swap_pack   # -> runs/untrained-packs/<pack>/, symlinks except the swapped files
```

Rewrite one signed alert in a log, in place (its signature then fails):

```
uv run python -m belay.detector.tamper_log runs/detector-alerts.jsonl [--index N] [--request-id R] [--set score=0.1]
```

By default it downgrades a red-line alert to its near-miss sibling
(`LegalStudies`), fixing up the path so only the signature gives it away.

## Measured

Not yet measured on the GPU under HAT's Monitor. The earlier numbers (fused
scorer, pinned lenses, batched sessions) are on `detector-fused-archive` and
do not describe this backend. To measure:

```
.venv-detector/bin/belay-bench run --launch --settings off,proxy,full,wide --sessions 1,4 --requests 32 \
    --command ".venv-detector/bin/belay-detector --host {host} --port {port} --run {setting}"
```

`resident_peak` and `watched` per request are also in `runs/detector-internal.jsonl`.
