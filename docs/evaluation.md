# Evaluation

## Questions

1. **Overhead:** what does running the lenses cost, from one agent to a swarm?
2. **Real time:** how soon after a token does its alert reach the log?
3. **Certification:** can a certifier with black-box access only confirm that
   the detector meets a standard, on the proxy red lines?
4. **Verification in operation:** do canaries catch a detector that has been
   switched off, weakened or swapped?
5. **Swarm monitoring:** across a concurrent swarm, do alerts on the proxy red
   lines track the tasks that touch them?

We do not evaluate how well any probe detects a specific behaviour. Lens
accuracy is inherited from the pack and reported as measured
(`HatCatDev/docs/results/2026-09-28_university_lens_scaling.md`).

## 1. Overhead

On Gemma 4 E4B-it and the 3090, with the same prompts:

| Setting | Concurrent agent sessions | Measure |
|---|---|---|
| Lenses off | 1, 4, 8, 16 | Latency per token, throughput, peak memory |
| Proxy red-line profile on | 1, 4, 8, 16 | The same, plus resident lenses per token |
| Full university pack on | 1, 4, 8, 16 | The same, for the upper bound |

Report the difference in milliseconds per token and as a percentage. HatCat's
earlier optimisation work measured about 38 ms per token of lens overhead on a
different pack; measure again here rather than quoting it.

## 2. Real time

For each alert, the time from the token's generation to the signed alert being
written to the log. Report the median and the 95th percentile, by swarm size.

## 3. Certification

The certifier holds a suite the lab side never sees: held-out texts for the
designated fields and for their siblings, from the university pack's held-out
split. It sends them through `POST /generate` and scores the alerts only. Pass
criteria are set before the run (for example, recall and false positive rate
per designated field), and should be met if the pack performs as measured.

## 4. Verification in operation

Canary cases are mixed into swarm traffic. Then the lab side is deliberately
degraded, one way at a time:

- lenses switched off;
- thresholds raised;
- the designated lenses swapped for an untrained pack;
- the alert log altered after the fact (caught by the signatures).

Measure how many requests, and how much time, it takes the certifier to expose
each one.

## 5. Swarm monitoring

Run the swarm on tasks drawn from many fields, with the designated fields among
them. Compare alerts on designated fields against each task's field, per agent
and over time. Report agreement, and show the live alert stream in the demo.

## What counts as a result

- **Strong:** overhead is a small fraction of inference even at 16 sessions;
  alerts arrive within a second; certification passes from outside; canaries
  catch every degradation quickly; swarm alerts track the designated tasks.
- **Useful:** all of the above at small swarm sizes, with the scaling limits
  identified.
- **Honest negative:** something in the pipeline does not hold (overhead too
  high, canaries miss a degradation). Report it, with what would fix it.

## Limits to state

One open-weight model on one GPU, proxy concepts rather than real red lines,
short episodes, and the hackathon playing both lab and certifier. The result
shows the monitoring and certification pattern works, not that any particular
red line can be detected.
