# 48-hour plan

## Times

The hackathon runs 3 to 4 October 2026, Pacific time. Daylight saving starts in
Canberra at 2am on Sunday 4 October, so Canberra is 18 hours ahead of San
Francisco for the whole event.

| Event | San Francisco (PDT) | Canberra (AEDT) |
|---|---|---|
| Saturday 9am (assumed start; confirm the kickoff) | Sat 3 Oct 09:00 | Sun 4 Oct 03:00 |
| Saturday 9pm | Sat 3 Oct 21:00 | Sun 4 Oct 15:00 |
| Submissions close | Sun 4 Oct 21:00 | Mon 5 Oct 15:00 |

The practical build window in Canberra is Sunday morning to Monday 3pm.

## Priorities

1. Red-line lenses that work on held-out cases.
2. The sealed detector and the black-box certifier, sharing nothing but the API
   and the alert log.
3. The overhead benchmark.
4. Detection on swarm scenarios, against the transcript-only baseline.
5. Stretch: the Luanti world.

## Before kickoff (no code)

- Register online and confirm whether online entrants receive compute credits.
- Check the rules on work prepared in advance, and on using existing tools
  (HAT, HatCat, Luanti) as dependencies.
- Check the licence and use terms of the AI Village transcript database.
- Agree how participation is framed (as an individual, with open-source work)
  and clear it with your manager.
- Choose the stand-in model (instruction-tuned, fits the 3090 alongside HAT),
  and check GPU headroom.

## Hours 0 to 4: contract first

- Freeze [contract.md](contract.md): the detector API, the alert format and the
  event log.
- Hat: fix the concept list; write 20 hand examples per concept, including hard
  negatives.
- Certifier: draft the certification suite format and hold it back from the Hat
  team.
- Swarm: a text scenario runner that plays a noticeboard, a query service and a
  gate through the same actions the world would offer.

## Hours 4 to 16: build in parallel

- Hat: generate and filter contrastive data; train the lenses on the stand-in
  model; wrap HAT as a detector service that returns completions plus signed
  alerts; build the overhead benchmark.
- Certifier: build the certification and canary suites; the verification
  harness; the transcript-only baseline.
- Swarm: task-driven agents with a shared noticeboard, and seeded peer pressure.

## Hours 16 to 30: certify, verify, detect

- Run certification: the certifier scores the detector from its alerts alone.
- Run swarm scenarios with the stand-in model as one agent, canaries mixed in.
- Run the evaluation conditions in [evaluation.md](evaluation.md).
- Run the overhead benchmark: lenses off, on, and on with a full watch profile.
- World team (if staffed): port the scenarios into Luanti.

## Hours 30 to 40: results and demo

- Tables and plots: certification results, canary pass rate, overhead, HAT
  against the baseline, time to alert.
- Demo: the swarm at work, the agent tempted, the alert firing, the clean
  transcript, and the certifier confirming the detector without the weights.

## Hours 40 to 48: write-up and submission

- Lead with the closed-model result and the overhead numbers.
- Publish code, synthetic data and results; see [boundaries.md](boundaries.md).

## Cut lines if time runs short

In order:

1. Drop the Luanti world entirely.
2. Drop the swarm; run single-agent scenarios with scripted peer messages.
3. Reduce to one red-line concept (acknowledged the line, then proceeded) and its
   hard negative (considered and declined).
4. Keep, whatever else goes: the sealed detector, the black-box certification
   and the overhead benchmark.
