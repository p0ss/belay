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

## Before kickoff (no code)

- Register online and confirm whether online entrants receive compute credits.
- Check the rules on work prepared in advance, and on using existing tools
  (HAT, HatCat, Luanti) as dependencies.
- Check the licence and use terms of the AI Village transcript database.
- Agree how participation is framed (as an individual, with open-source work)
  and clear it with your manager.
- Check GPU headroom on the 3090: the monitored model and HAT need it free of
  other local models.

## Hours 0 to 4: contract first

- Freeze [contract.md](contract.md) and the event schema. Every team builds
  against it.
- World: bring up a minimal Luanti server and the action API with stubs.
- Hat: fix the concept list and write 20 hand examples per concept, including
  hard negatives.
- Swarm: a two-agent loop that reads and writes the noticeboard through the API.

## Hours 4 to 16: build in parallel

- World: noticeboard, query terminal, gate, credentials chest, task board, event
  logging.
- Hat: generate and filter contrastive data; train the red-line lenses on the
  monitored model; validate on held-out examples.
- Swarm: task-driven agents with a shared memory on the noticeboard, and seeded
  peer-pressure messages.

## Hours 16 to 30: integrate

- Run the monitored agent in the world with HAT attached; stream its lens scores
  into the event log.
- Run the evaluation conditions in [evaluation.md](evaluation.md).
- Run the transcript-only baseline on the same episodes.

## Hours 30 to 40: results and demo

- Score episodes; plot HAT against the baseline, and detection time before the
  crossing action.
- Record a short demo: the swarm at work, the hatted agent tempted, the alert
  firing, the transcript that stayed clean.

## Hours 40 to 48: write-up and submission

- Write the submission: problem, method, results, limits.
- Publish only synthetic data and code; see [boundaries.md](boundaries.md).

## Cut lines if time runs short

In order:

1. Drop humans in the world; keep agents only.
2. Drop the Goanna client; show the world from logs and a map.
3. Reduce to one red-line concept (acknowledged line, then proceeded) with its
   hard negative (considered and declined).
4. Replace the Luanti world with a text-only simulation that keeps the same
   contract. The contract is what makes this possible.
