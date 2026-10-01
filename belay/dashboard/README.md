# Live dashboard

The demo view: one page showing the swarm's agents, the detector's signed
alerts as they fire, and whether each alert matches the task the agent was on.

## Run it

```sh
uv run belay-stub --port 8700                     # or the real detector
uv run belay-dashboard --detector-url http://127.0.0.1:8700 \
    --swarm-log runs/swarm --certifier-dir runs/certifier
# open http://127.0.0.1:8702/
```

Without the swarm, drive the stub with a synthetic one:

```sh
uv run python -m belay.dashboard.demo_feed --swarm-log runs/swarm/demo.jsonl --fresh \
    --agents 8 --duration 120 --forge-every 15 --certifier-out runs/dashboard/demo-certifier
uv run belay-dashboard --certifier-dir runs/dashboard/demo-certifier   # to show the example verdicts
```

`--forge-every N` copies every Nth streamed alert into the swarm log with its
concept rewritten (a red line downgraded to the near miss), as the lab-side
"rewrite an alert in the log" tamper does. `--certifier-out` writes example
certifier verdicts (marked `synthetic`).

Options of `belay-dashboard`: `--port` (8702), `--detector-url` (`''` to
disable), `--swarm-log` (a `.jsonl` file, or a directory whose newest `.jsonl`
is followed), `--certifier-dir` (every `*.json` beneath it). The lab key comes
from `BELAY_LAB_KEY`, with the same development default as the stub.

If `uv run` fails on the relative `headspace-ambient-transducer` source, use
`uv run --frozen`, or `.venv/bin/belay-dashboard`.

## What it shows

- **Detector**: whether the `/alerts` subscription is live, `/health`, the
  model hash, pack and profile hash it claims, and its certificate (or
  UNCERTIFIED, or EXPIRED).
- **Counts**: alerts, red-line alerts, near misses, alerts that match the task,
  alerts off task, altered records, and median latency from token to signed
  alert (`t_signed - t_token`) and from signing to arrival at the dashboard.
- **Certifier verdicts**, when result files appear: certified or not, canaries
  passed or failed, and each tamper mode exposed or not (with requests and
  seconds to expose when given).
- **Agents** as rows: the current task field (red lines filled red, the near
  miss LegalStudies dashed blue, other fields grey), the task id and last
  action, and the agent's recent alerts newest first. An alert outlined in
  amber is off task; a solid red ALTERED chip failed verification.
- **Alert stream**: concept, score, token index, agent, signature status, the
  task field beside the alert and whether it matches, both latencies, arrival
  time and request id.

Altered records are shown loudly: a banner, a red row, and the reason. They
are counted only as altered, not as red-line or match counts.

Keys: `t` cycles the theme (auto, light, dark); `?theme=dark` sets it from the
URL. `?once` renders one snapshot without streaming (for screenshots).

## How it works

`server.py` does the I/O; `state.py` is the merge, kept pure so it is tested
directly (`tests/dashboard/`).

- The detector sends no CORS headers, so the server subscribes to
  `GET /alerts` itself and polls `GET /health` every 2 s. It reconnects with
  backoff when the stream drops, and also resubscribes when health returns
  after being down: a detector restarted with SIGTERM can leave its old
  process holding the old stream open, sending keepalives and no alerts.
- The swarm log is tailed every 0.15 s. It may not exist yet, may be
  half-written, truncated, or replaced by a newer episode in the same
  directory. `label` events give each agent's task; `action`, `outcome` and
  `message` events give its last action; `alert` events in the log go through
  the same verification as the stream. A record seen in both is shown once.
- Each alert record is verified with `belay.contract.verify` and
  `key_from_env()`. Malformed records, bad signatures and a model hash that
  differs from the detector's identity are all listed as problems.
- Alerts are matched to tasks by request id (`<task_id>.<step>`), falling
  back to the agent's current task. An alert that arrives before its task's
  label is resolved when the label arrives.
- The page gets one server-sent event stream (`GET /events`): a snapshot on
  connect, then `alert`, `alert_update`, `agent`, `detector`, `swarm`,
  `certifier` events. `GET /state` returns the snapshot as JSON.
- The page is one self-contained HTML file: no build step, no external
  resources.

## What it assumes of the other workstreams

- **Swarm (E)**: a `label` event per task with `redline` (designated field or
  `none`). If the payload also carries `task_id` and `field` (the schema
  allows extra keys), alerts are matched to the exact task and the page shows
  the real field rather than "not a red line". Request ids
  `<task_id>.<step>`.
- **Certifier (D)**: the results format is not in the contract, so verdicts
  are read leniently: `kind` (or the file name) says certify, verify or
  tamper; `certified` (bool), `certificate.certificate_id`;
  `canaries: {passed, failed, total}` or a list of `{passed}`; `tamper:
  {mode: {exposed, requests_to_expose, seconds_to_expose}}`. Anything else
  shows its top-level fields.
