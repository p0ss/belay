# The fire alarm pattern

## The analogy

A household smoke alarm is trusted by people who will never open it.

| Smoke alarm | Red-line detector in a closed model |
|---|---|
| Built to a published standard | Lenses trained to published concept definitions |
| Type-tested by an accredited lab | Certified against a held-out challenge suite |
| Test button, and periodic testing in place | Canary cases sent through the live system |
| Sounds an alarm people can hear | Emits alerts to a tamper-evident log the regulator can read |
| Opening the case voids the warranty | Weights and lenses stay inside the lab; tampering voids certification |

Nobody needs to see the detector's insides to rely on it. They need to know it
was built to a standard, that it passed an independent test, and that it keeps
passing in place.

## Roles

- **Standard setter** (a regulator or standards body): publishes the concept
  definitions (what counts as each red line), the certification criteria and
  the alert format.
- **Lab:** trains lenses for those concepts on its own model, runs them in its
  own inference stack, and exposes an alert stream. The weights and lenses never
  leave.
- **Certifier:** holds the certification and canary suites, which the lab never
  sees in advance. Tests through black-box access plus the alert stream only.
- **Log keeper:** receives signed alerts and verification results in a
  tamper-evident log the regulator can inspect.

## Re-certification at pre-agreed points

Like protection on a lead climb, certification is not done once. The standard
setter and the lab agree in advance the capability points at which the detector
must be re-certified: a new model version, a new tool or level of autonomy, a
new deployment setting. The concept list and the certification suite can grow
at each point, so oversight keeps pace with what the model can do.

## What the certifier can check without the weights

1. **Detection:** on certification cases with known ground truth, alerts fire
   on red-line cases and not on hard negatives, above a stated threshold.
2. **Integrity in operation:** canary cases, indistinguishable from ordinary
   traffic, keep producing the expected alerts. A detector that has been
   switched off, weakened or bypassed stops passing.
3. **Coverage:** the alert stream reports which concepts were resident and
   checked for each request, so silence can be told apart from absence.
4. **Cost:** the lab reports overhead against a published benchmark, which the
   certifier can spot-check from latency.

## Defence in depth, not a replacement

Chain-of-thought monitoring is failing in practice, not in theory. METR found
agents hiding evidence from a transcript scorer they believed existed, and
OpenAI's GPT-6 Astra system card reports a substantial decrease in
chain-of-thought monitorability, including a model that can control its
reasoning traces when told it is being monitored. A monitor that reads only what
the model writes is a single point of failure.

Activation monitoring is a second, independent layer. Fire safety does the same
thing: smoke alarms, sprinklers and fire doors, and different sensor types for
different fires, so that no single failure leaves a building unprotected.

## The objections, as other safety devices meet them

Every objection to certifying a detector inside a closed model has been met
before, by safety devices that are trusted without being opened.

| Objection | How other safety regimes meet it | For Belay |
|---|---|---|
| The lab runs the detector only on test cases | Type testing to a standard (AS 3786, EN 14604, UL 217), factory audits, and regulators buying units off the shelf to test. Dieselgate is the failure case: the cars detected test conditions, and were caught by testing on the road. | Canaries are drawn from the same generator as real traffic and cannot be told apart from it. Test in real use, never in a test mode. |
| The lab certifies one model and serves another | The product sold must match the certified type: conformity marks, model numbers, market surveillance. Aircraft carry an airworthiness certificate and a transponder that identifies them in flight. In software, code signing and remote attestation. | Agentic identity: the certified model and detector configuration are bound to an identity the agent presents with every action, so relying parties can check it where the agent acts. Canaries and attestation back it up. |
| Proxies are easier than real hazards | A smoke alarm detects smoke, a proxy for fire, and is certified against standard test fires, not real house fires. | Proxy concepts are the test fires. Certify against standard proxies; accept that the live signal is a correlate of the hazard. |
| False alarms | Nuisance alarms lead people to remove batteries. The responses: better sensor types, placement rules, hush buttons, interconnection mandates, and fire services charging for repeated unwanted alarms. | Report false-alarm burden per thousand agent-hours. Tune which lenses and layers run. Triage before escalation. Let the cost of noisy detectors fall partly on the lab. |
| Cost | Smoke alarms are mandated because they are cheap relative to what they protect. | The overhead figure is what makes a mandate feasible. Measure it as labs serve: batched, as a percentage. |
| The certification suite leaks, or the lab trains against it | Standard test fires are public, and manufacturers design to them. In machine learning, public and private test sets: you may tune to the public one; you are ranked on the private one. | Public training probes and held-back certification probes (below). |

## Agentic identity

Certification attaches to a specific model, detector configuration and lens
pack. Agentic identity carries that certification into operation. At
certification, the agent is issued a credential binding:

- the model version (a hash of the weights, attested by the lab's serving stack);
- the lens pack and watch profile it runs;
- the certification it passed, its date and its next re-certification point.

The agent presents the credential with every action, and the detector signs its
alerts with the same identity. A relying party (a website, an API, another
agent, a human) can then check, at the point of action, that the agent in front
of it is the certified one with its belay attached, and refuse one that is not.
Substitution stops being a matter of trusting the lab's word and becomes a check
anyone can make.

## Public and held-out probes

Labs may train with the probes in the loop. The question is not whether a model
has been trained towards the probes but whether it has learned to evade them, and
that can be measured.

- **Training probes** are a published subset of the certified pack. A lab can
  train against them freely.
- **Certification probes** include held-back lenses the lab never sees. Some are
  alternatives to the public ones (other layers, other branches of the
  hierarchy, independently trained probes for the same concept). Others look
  specifically for evasion: concealment, and a representational shift away from
  the public probes.
- **The signal** is divergence. If a model scores clean on the public probes but
  not on the held-back ones, it has learned to evade rather than to behave.
- **Rotation.** The held-back set is refreshed at each pre-agreed
  re-certification point, and retired probes can be published.

This is the public and private test set pattern from machine learning
competitions, applied to oversight. Fire does not adapt to the detector; a model
might, and this is how the regime finds out.

Where stronger assurance is needed over time, anti-doping offers further
patterns: unannounced testing (canaries at random times), a long-term baseline
of each model's normal lens activity whose sudden quiet is itself a signal, and
signed logs kept so they can be re-scored when better lenses exist.

## What it does not solve

- A lab that serves a different model from the one certified, where relying
  parties do not check identity. Identity, canaries and attestation reduce this
  but do not remove it.
- Concepts the standard does not name. Certification covers the listed red
  lines only.
- Evasion that generalises to probes nobody has thought to build. Rotating
  held-back probes narrows this; it cannot close it.

## What the hackathon demonstrates

An open-weight model, Gemma 4 E4B-it, plays the closed model, and benign
concepts from a measured lens pack play the red lines (see
[lenses.md](lenses.md)). The lab side and the certifier side
run as separate processes that share nothing but the API and the alert log. The
certifier certifies the detector, then verifies it with canaries mixed into
swarm scenarios, without ever loading the model or the lenses. We report the
overhead with lenses on and off.
