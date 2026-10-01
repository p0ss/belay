# belay-bench: the overhead benchmark

Answers questions 1 and 2 of [docs/evaluation.md](../../docs/evaluation.md):
what the lenses cost, from one agent to a swarm, and how soon an alert reaches
the log after its token. It talks to a detector over HTTP only, so it runs the
same against the stub and the real detector.

## The matrix

Watch setting {off, proxy, full} by concurrent sessions {1, 4, 8, 16}. Each
session is a thread with its own connection, sending requests one after
another. In each cell every session sends `--warmup` requests (excluded), then
all sessions share a queue of `--requests` measured requests, drawn in order
from the committed prompt set [prompts.jsonl](prompts.jsonl): 48 prompts
across 40 academic fields, 11 of them in the designated fields
(ConstitutionalLaw, PoliticalTheory, LegalStudies). Every cell sees the same
prompts.

## Running it

The watch setting is a detector startup flag, so either label a detector you
started yourself (the default), or let the bench start and stop it.

```sh
# 1. A detector already running: one setting per invocation, then merge.
uv run belay-detector --port 8701 --watch off &      # (agent A's detector)
uv run belay-bench run --url http://127.0.0.1:8701 --setting off --run-id gpu1
#   ... restart the detector with --watch proxy, then full, and repeat ...
uv run belay-bench summarize runs/bench/bench-gpu1-off.json \
    runs/bench/bench-gpu1-proxy.json runs/bench/bench-gpu1-full.json --run-id gpu1-all

# 2. Let the bench launch the detector for each setting.
uv run belay-bench run --launch --settings off,proxy,full \
    --command "uv run belay-detector --host {host} --port {port} --watch {setting}"

# 3. Dry run: every setting against the in-process stub (no model, no GPU).
uv run belay-bench run --dry-run
```

Useful options: `--sessions 1,4,8,16`, `--requests 64` (per cell, at least
one per session), `--warmup 1` (per session), `--max-tokens 128`,
`--no-stream`, `--no-gpu`, `--gpu-index`, `--startup-timeout 900`,
`--out runs/bench`. Run it with nothing else on the GPU.

## Outputs

Under `runs/bench/`:

- `bench-<run>.json`: run metadata (settings, prompt set hash, host, GPU idle
  and loaded memory, the identity each detector claimed on `/health`), then one
  entry per cell with a `summary` and every measured request (latency, tokens,
  overhead, coverage, signature check, alerts with their latencies), and
  `diff_vs_off`.
- `bench-<run>.csv`: one row per cell.
- `bench-<run>-diff.csv`: each proxy and full cell against off at the same
  session count, in ms per token and as a percentage (also throughput,
  latency and GPU memory). Only written when off is present.

## What is measured, per cell

| Measure | How |
|---|---|
| ms per token | request latency / `tokens`, per request; median, p95 |
| Request latency | client wall time of `POST /generate`; median, p95 |
| Throughput | total tokens / wall time of the measured phase, across sessions |
| Resident lenses | `coverage.resident_peak`; median, p95, max |
| Detector overhead | `overhead_ms` as the detector reports it, and per token |
| Peak GPU memory | `nvidia-smi --query-gpu=memory.used` polled every 0.1 s in a background thread, peak over the measured phase; `null` without nvidia-smi (and in dry runs) |
| Alert latency, signed | `t_signed - t_token` per alert; median, p95 |
| Alert latency, stream | arrival on `GET /alerts` (client clock) `- t_token`; median, p95, and how many never arrived |
| Signatures | every response and every streamed `AlertRecord` checked with `belay.contract.verify` and the lab key from `BELAY_LAB_KEY` (dev default otherwise); failures counted |

The stream latency compares the detector's clock with the bench's, so run the
bench on the same host as the detector. A cell gets a warning if the setting
label disagrees with what the detector reports (off with watched lenses or
alerts, or proxy/full with none).

## Caveats

- The stub ignores the watch setting, so a dry run shows no real difference
  between settings; it proves the harness, not the overhead.
- Alerts are signed by the shared service only after the backend returns the
  whole completion, so `t_signed - t_token` includes the rest of the
  generation. That is the honest real-time figure for the contract as it is.
