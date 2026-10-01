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

## What it does not solve

- A lab that runs a different model for certification from the one it serves.
  Canaries in live traffic, and attestation of the serving stack, reduce but do
  not remove this.
- Concepts the standard does not name. Certification covers the listed red
  lines only.
- Adversarial training against the lenses. Hierarchy helps (suppressing a leaf
  still has to pass its parents and siblings), but this needs its own study.

## What the hackathon demonstrates

An open-weight model plays the closed model. The lab side and the certifier side
run as separate processes that share nothing but the API and the alert log. The
certifier certifies the detector, then verifies it with canaries mixed into
swarm scenarios, without ever loading the model or the lenses. We report the
overhead with lenses on and off.
