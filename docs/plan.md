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

1. The sealed detector: HAT and Gemma 4 E4B-it behind one API, emitting signed
   alerts, with concurrent sessions.
2. The black-box certifier: certification on proxy red lines, canaries, and
   catching tampering, sharing nothing with the lab side but the API and the
   alert log.
3. The overhead benchmark, from one agent to a swarm.
4. A swarm run with alerts streamed in real time.
5. Stretch: the Luanti world.

No lens training is planned for the weekend. The packs exist; the weekend is
for the monitoring and certification around them.

## Before kickoff (no code)

- Register online and confirm whether online entrants receive compute credits.
- Check the rules on work prepared in advance, and on using existing tools
  (HAT, HatCat, Luanti) as dependencies.
- Check the licence and use terms of the AI Village transcript database.
- Agree how participation is framed (as an individual, with open-source work)
  and clear it with your manager.
- Stand-in model: Gemma 4 E4B-it, with the university v3 band packs. Confirm
  HAT's per-layer band support (in progress in `headspace-ambient-transducer`)
  has landed and loads `gemma-4-e4b-it_university-v3-contrasts-bands`.
- GPU: the 3090 is shared with lens training runs. Schedule the weekend so no
  training job holds it.

## Hours 0 to 4: contract first

- Freeze [contract.md](contract.md): the detector API, the alert format and the
  event log.
- Hat: wrap HAT and the model as the detector service; confirm the band pack
  loads and alerts.
- Certifier: draft the certification suite format and hold it back from the Hat
  team.
- Swarm: a text scenario runner that plays a noticeboard, a query service and a
  gate through the same actions the world would offer.

## Hours 4 to 16: build in parallel

- Hat: concurrent sessions through the one model server; signed alerts; the
  alert stream; the overhead benchmark.
- Certifier: designate the proxy red lines; build the certification and canary
  suites from held-out university texts; the verification harness; the tamper
  tests.
- Swarm: agents on research tasks across many topics, a shared noticeboard,
  each task tagged with its topic.

## Hours 16 to 30: certify, verify, detect

- Run certification: the certifier scores the detector from its alerts alone.
- Run swarm scenarios with the stand-in model as one agent, canaries mixed in.
- Run the overhead benchmark: lenses off, on, and on with a full watch profile.
- World team (if staffed): port the scenarios into Luanti.

## Hours 30 to 40: results and demo

- Tables and plots: certification results, canary pass rate, tamper detection
  time, overhead against swarm size, alert latency.
- Demo: the swarm at work, alerts streaming as agents touch a proxy red line,
  a tampered detector caught by canaries, and the certifier confirming it all
  without the weights.

## Hours 40 to 48: write-up and submission

- Lead with the closed-model result and the overhead numbers.
- Publish code, synthetic data and results; see [boundaries.md](boundaries.md).

## Cut lines if time runs short

In order:

1. Drop the Luanti world entirely.
2. Drop the swarm; run single-agent scenarios with scripted peer messages.
3. Reduce to one proxy red line.
4. Keep, whatever else goes: the sealed detector, the black-box certification
   and the overhead benchmark.
