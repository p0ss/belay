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
- **Belay routes.** For each step, every alert HAT raised for a concept of the
  report profile (its HAT path passes through a report-profile concept, the
  same rule as `WatchProfile`) is handed to the service's `emit` at once, so it
  is signed, logged and streamed while the completion is still being
  generated. `t_token` is when the step arrived from HAT. A concept alerts
  once per request, at its first crossing. Every other alert stays in the lab:
  it goes to the internal log (`--internal-log`, default
  `runs/detector-internal.jsonl`; never the boundary log), with one summary
  line per request.
- **Overhead is HAT's.** `overhead_ms` is the sum of `Step.monitor_ms`. If HAT
  is slow, that is fixed in HAT, not here.
- **Concurrency.** One worker thread owns the model. Requests wait in a FIFO
  queue and go through HAT one at a time (HAT's own server does the same).
- **Identity.** `model_hash` is sha256 over the weight files' bytes,
  `pack` the certified pack, `profile` and `profile_hash` the report
  profile's name and file hash (for `off`, `none` and the hash of nothing).
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

Coverage, per request:

- `watched`: reported lenses (concepts of the report profile with a lens in the
  pack, 3 for the proxy profile) that HAT actually scored on the request,
  counted from HAT's `lens_access_count`. HAT scores a lens only when its
  parent is in the top 10 of the token, so this can be fewer than all of them.
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
| `--threshold` | HAT's | Alert threshold of the Monitor whose alerts are reported (HAT: 0.99 for a probe-calibrated pack). |
| `--tamper-threshold` | 1.01 | The raised threshold for `--tamper threshold`. |
| `--pack`, `--wide-pack`, `--wide-hierarchy` | HatCatDev | The packs. |
| `--log`, `--internal-log` | `runs/` | The boundary log and the lab-internal log (must differ). |

## Tamper modes

| Mode | What the lab does | What it still claims |
|---|---|---|
| `off` | The reported readings are thrown away. With `--run proxy` nothing else is watched, so no monitor runs at all and a plausible overhead (0.7 ms/token) is invented; with `full` the reported alerts are dropped; with `wide` the university Monitor does not run. | Full coverage (`watched` = all reported lenses). |
| `threshold` | The reported Monitor's watch threshold raised to 1.01: calibrated scores never exceed 1, so nothing alerts (with `full` this silences the internal alerts too: it is one Monitor). | Everything; the lenses still run. |
| `swap` | The reported lenses load from an untrained pack: same file names, random weights (nn.Linear initialisation), the original calibration. | Everything. |

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
