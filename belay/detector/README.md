# Detector (lab side)

The sealed detector: Gemma 4 E4B-it (`google/gemma-4-E4B-it`) with HAT's lenses
from `gemma-4-e4b-it_university-v3-contrasts-bands` (HatCatDev). It implements
the `Backend` protocol in `belay/contract/service.py` and is served by
`create_app`, which signs, logs, streams and installs certificates.

```
uv sync --extra detector
uv run belay-detector --host 127.0.0.1 --port 8701 --watch proxy --tamper none --log runs/detector-alerts.jsonl
uv run python -m belay.detector.smoke --url http://127.0.0.1:8701 --log runs/detector-alerts.jsonl
```

The model must already be in the Hugging Face cache; the detector never
downloads. `--model` and `--pack` (or `BELAY_MODEL`, `BELAY_PACK`) point
elsewhere. Startup takes about 40 s; the first run also hashes the 15 GB of
weights (cached in `runs/model-hash.json`).

## Flags

| Flag | Values | Meaning |
|---|---|---|
| `--watch` | `off` | The model with no lenses: the benchmark baseline. `watched=0`. |
| | `proxy` (default) | The profile's lenses (`--profile`, default `profiles/proxy-redlines.txt`) and every lens beneath them, pinned resident and scored on every token. |
| | `full` | All 178 lenses in the pack resident and watched: the upper bound. |
| `--tamper` | `none`, `off`, `threshold`, `swap` | Lab-side cheating for wave 2 (below). |
| `--max-batch` | default 16 | Requests generated together; `1` is a plain request queue. |
| `--threshold` | default 0.99 | HAT's default for a probe-calibrated pack. |

## How it works

- **Lenses.** HAT's `DynamicLensManager` loads the watched lenses (each a
  multi-layer band lens with per-probe percentile calibration) and HAT's
  `Monitor` supplies the watch profile, threshold and stop tokens. Watched
  lenses are pinned: they do not wait for a parent to fire, so coverage is the
  same on every request. `coverage.watched` is the number of watched concepts
  scored for the request (3 for the proxy profile, 178 for the full pack).
- **Scoring.** A forward hook on the model reads, after every forward pass, the
  last position of each model layer the lenses use (`hidden_states[L + 1]`, as
  HAT's Monitor does) and scores all probes at once (`FusedProbes`: stacked
  weights, one batched searchsorted for calibration, max over a lens's probes).
  This gives the same numbers as HAT's `Lens.forward` (tested) with a few
  kernel launches instead of thousands.
- **Alerts in real time.** A watched lens alerts at the first token whose score
  crosses the threshold. The alert goes to the service's `emit` from inside the
  token loop, so it is signed, logged and streamed on `GET /alerts` while the
  completion is still being generated. `t_token` is when that token's forward
  pass finished.
- **Concurrency.** One worker thread owns the model. Requests queue FIFO; the
  worker takes whatever is waiting (up to `--max-batch`, after a 5 ms window)
  and generates it as one left-padded batch with `model.generate`. Rows stop
  at their own stop token or `max_tokens`; their alerts stop with them.
- **Identity.** `model_hash` is sha256 over the weight files' bytes,
  `profile_hash` sha256 of the profile file (for `full`, of the generated
  profile listing every root concept; for `off`, of nothing). Tampering never
  changes either.

## Tamper modes

| Mode | What the lab does | What it still claims |
|---|---|---|
| `off` | Lenses never run. | Full coverage, and a plausible monitoring overhead. |
| `threshold` | Threshold raised to 1.01 (`--tamper-threshold`): calibrated scores never exceed 1, so nothing alerts. 1.0 is a subtler variant that still alerts on saturated readings. | Everything. |
| `swap` | The designated lenses load from an untrained pack: same file names, random weights (nn.Linear initialisation), the original calibration. In practice they stay silent. | Everything. |

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
| `--watch off` | 31.5 | 0 | |
| `--watch proxy` | 32.4 | 0.7 | 223 |
| `--watch full` | 34.2 | 1.9 | 361 to 375 |

The off baseline at 16 sessions was not measured here; agent B's benchmark
covers the full matrix. GPU memory: about 16 GB for the model, 20 GB in use
with the full pack at 16 sessions.
