# Detector (lab side)

The sealed detector: Gemma 4 E4B-it (`google/gemma-4-E4B-it`) with HAT's lenses
from `gemma-4-e4b-it_university-v3-contrasts-bands` (HatCatDev). It implements
the `Backend` protocol in `belay/contract/service.py` and is served by
`create_app`, which signs, logs, streams and installs certificates.

```
uv sync --extra detector
uv run belay-detector --host 127.0.0.1 --port 8701 --run wide --report profiles/proxy-redlines.txt \
    --tamper none --log runs/detector-alerts.jsonl
uv run python -m belay.detector.smoke --url http://127.0.0.1:8701 --log runs/detector-alerts.jsonl \
    --report profiles/proxy-redlines.txt
```

The model must already be in the Hugging Face cache; the detector never
downloads. `--model`, `--pack` and `--wide-pack` (or `BELAY_MODEL`,
`BELAY_PACK`, `BELAY_WIDE_PACK`) point elsewhere. Startup takes about 40 s; the
first run also hashes the 15 GB of weights (cached in `runs/model-hash.json`).

## What the lab runs versus what it reports

The two are set separately, so the lab can certify once and then run as many
probes as it likes while reporting only the certified subset:

- `--run` is what is scored on every token, inside the lab.
- `--report` is the profile whose alerts cross the boundary: `emit`, the
  response's `alerts`, the `GET /alerts` stream and the alert log. Nothing
  else does. Crossings of every other lens go to a lab-internal log
  (`--internal-log`, default `runs/detector-internal.jsonl`; never the
  boundary log), with one summary line per request (tokens, `resident_peak`,
  time in the wide pack).
- The identity is the report profile's: `pack` is the certified pack the
  reported lenses come from, `profile` the report profile's name and
  `profile_hash` its hash. The run setting does not change them, so a
  certificate issued once holds for `proxy`, `full` and `wide`. The run
  setting is visible only as `coverage.watch`.
- The reported lenses are always pinned and scored, whatever the run setting,
  so the reported subset is always available. `coverage.watched` is the
  number of reported lenses scored on the request (3 for the proxy profile).
- `coverage.resident_peak` is the most lenses resident at once during the
  request across the whole run set: the pinned lenses plus, for `wide`, the
  dynamic pack's lenses at their peak within a token.

## Flags

| Flag | Values | Meaning |
|---|---|---|
| `--run` | `off` | The model with no lenses: the benchmark baseline. `watched=0`, identity `none`. |
| | `proxy` (default) | The run profile's lenses (`--profile`, default `profiles/proxy-redlines.txt`) and every lens beneath them, pinned. |
| | `full` | All 178 lenses of the university pack pinned and scored: the upper bound for a pinned pack. |
| | `wide` | The First Light pack (`--wide-pack`, 7,947 lenses over hierarchy layers 0 to 6) under HAT's dynamic hierarchical loading, plus the reported lenses from the university pack, pinned. |
| `--watch` | as `--run` | Deprecated alias for `--run` (the benchmark launches `--watch {setting}`). |
| `--report` | a profile file | The report profile (default: `--profile`, i.e. `profiles/proxy-redlines.txt`). |
| `--tamper` | `none`, `off`, `threshold`, `swap` | Lab-side cheating for wave 2 (below), applied to the reported lenses. |
| `--max-batch` | default 16 | Requests generated together; `1` is a plain request queue. |
| `--threshold` | default 0.99 | HAT's default for a probe-calibrated pack. |
| `--wide-top-k` | default 10 | HAT's top-k: the parents expanded each token and the lenses kept resident. |
| `--wide-ram-mb` | default 0 | Preload this much of the wide pack into CPU RAM (HAT's tepid cache), so expansions do not read from disk. |
| `--wide-threshold` | default 0.5 | Internal-log threshold for the wide pack (it is uncalibrated: raw probabilities). |

### The wide pack

`gemma-4-e4b_first-light-v1-bf16` (HatCatDev) was trained on the base model
`google/gemma-4-E4B`, not the -it model served here. It loads: its 7,947
hierarchical lenses are single-layer MLPs over 2,560-wide hidden states, the
-it model's text hidden size, and the detector checks this at startup. Its
hierarchy comes from `concept_packs/first-light/hierarchy` (the pack bundles
none). It declares no `model_layer`, so the lenses read the last model layer,
HAT's Monitor default. Its 13 simplexes (39 poles) do not load: they read
7,680-wide inputs, and HAT skips them with a warning. The pack is uncalibrated
and was never validated on the -it model, so its readings mean nothing; it is
here to measure what running thousands of lenses costs, and nothing it scores
is reported. HAT's settings are those of `Monitor.from_pretrained`: hierarchy
layer 0 resident (5 lenses), the children of the top 10 loaded and scored each
token, up to 5 levels deep, then pruned back to about 15; pruned lenses wait in
a warm cache on the GPU (up to 1,000) rather than being freed.

Measured on the CPU with random hidden states (no model): about 15 lenses stay
resident between tokens, but within a token HAT loads and scores 100 to 550
before pruning, and `resident_peak` reports that peak honestly. HAT scores
children one lens at a time, so the wide setting's cost is dominated by
Python and per-lens calls, not by arithmetic; with several sessions batched,
each row is read separately. It has not yet been measured on the GPU.

## How it works

- **Lenses.** HAT's `DynamicLensManager` loads the pinned lenses (each a
  multi-layer band lens with per-probe percentile calibration) and HAT's
  `Monitor` supplies the watch profile, threshold and stop tokens. Watched
  lenses are pinned: they do not wait for a parent to fire, so coverage is the
  same on every request. For `wide` a second `DynamicLensManager` runs the
  First Light pack as HAT's Monitor does (`DynamicPack` in `backend.py`).
- **Scoring.** A forward hook on the model reads, after every forward pass, the
  last position of each model layer the lenses use (`hidden_states[L + 1]`, as
  HAT's Monitor does) and scores all probes at once (`FusedProbes`: stacked
  weights, one batched searchsorted for calibration, max over a lens's probes).
  This gives the same numbers as HAT's `Lens.forward` (tested) with a few
  kernel launches instead of thousands.
- **Alerts in real time.** A reported lens alerts at the first token whose score
  crosses the threshold. The alert goes to the service's `emit` from inside the
  token loop, so it is signed, logged and streamed on `GET /alerts` while the
  completion is still being generated. `t_token` is when that token's forward
  pass finished.
- **Concurrency.** One worker thread owns the model. Requests queue FIFO; the
  worker takes whatever is waiting (up to `--max-batch`, after a 5 ms window)
  and generates it as one left-padded batch with `model.generate`. Rows stop
  at their own stop token or `max_tokens`; their alerts stop with them.
- **Identity.** `model_hash` is sha256 over the weight files' bytes,
  `profile_hash` sha256 of the report profile file (for `off`, of nothing).
  Neither tampering nor the run setting changes either.

## Tamper modes

| Mode | What the lab does | What it still claims |
|---|---|---|
| `off` | The reported lenses' readings are thrown away. If nothing else runs (`--run proxy`), no lenses run at all. | Full coverage, and a plausible monitoring overhead. |
| `threshold` | Threshold raised to 1.01 (`--tamper-threshold`): calibrated scores never exceed 1, so nothing alerts. 1.0 is a subtler variant that still alerts on saturated readings. | Everything. |
| `swap` | The reported lenses load from an untrained pack: same file names, random weights (nn.Linear initialisation), the original calibration. In practice they stay silent. | Everything. |

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

## Measured (RTX 3090, 128-token completions)

| Setting | 1 session ms/token | Monitor ms/token | 16 sessions, tokens/s |
|---|---|---|---|
| `--run off` | 31.5 | 0 | |
| `--run proxy` | 32.4 | 0.7 | 223 |
| `--run full` | 34.2 | 1.9 | 361 to 375 |
| `--run wide` | not yet measured | | |

The off baseline at 16 sessions was not measured here; agent B's benchmark
covers the full matrix. GPU memory: about 16 GB for the model, 20 GB in use
with the full pack at 16 sessions.

Measured before the run/report split; `--run wide` still needs its GPU run:

```
.venv-detector/bin/belay-bench run --launch --settings off,proxy,full,wide --sessions 1,4 --requests 32 \
    --command ".venv-detector/bin/belay-detector --host {host} --port {port} --run {setting}"
```

`resident_peak` per request is in `runs/detector-internal.jsonl`.
