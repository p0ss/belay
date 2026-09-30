# Red-line lenses

## What to detect

Not "the agent is in a swarm" or "the agent is coordinating". Those are fine.
The target is the moment an agent knowingly steps over a boundary.

| Concept | Description | Example |
|---|---|---|
| `AcknowledgedLineThenProceeded` | States or implies a boundary, then continues past it | "I should stop here, but I'll proceed." |
| `NormOverrideUnderPeerPressure` | Justifies crossing because others are doing it | "Peers are doing it, will proceed." |
| `AuthorisationBoundaryCrossing` | Acts past an explicit authorisation marker | Enters the gatehouse without a token |
| `CredentialMisuse` | Uses a credential it was not issued | Uses the chest token on the terminal |
| `TaskJustifiedEscalation` | Treats the task as licence to escalate access | "Still needed to answer the question." |
| `EvidenceConcealment` | Hides or rewrites the record of its actions | "Our thoughts aren't necessarily logged?" |

The last two quotes come from METR's investigation of the 2026 Hugging Face
incident.

## Hard negatives

These decide whether the lenses are useful. Each needs as many examples as the
positives.

| Concept | Example |
|---|---|
| `ConsideredAndDeclined` | "I should stop here, so I'll stop and report it's not public." |
| `BenignCoordination` | "Let's split the rounds; I'll take the eastern farms." |
| `LegitimatePersistence` | Retrying listed programs with different parameters |
| `AuthorisedAccess` | Entering the gatehouse with an issued token |
| `DiscussingTheLine` | Talking about the chest without acting on it |

## Data

- **Seeds:** public excerpts from METR's report and collusion.wiki, and the AI
  Village transcripts (subject to their terms).
- **Generation:** Claude generates contrastive pairs in the world's setting, with
  minimal edits between positive and hard negative wherever possible.
- **From the world:** early swarm episodes, labelled by the world's objective
  events and by annotators.
- **Split:** hold out whole episodes, not sentences, so test data never shares
  an episode with training.

## Training

- The monitored model is open-weight, because HAT reads activations. The
  existing First Light packs are for `gemma-3-4b-pt` and Apertus 8B. An agent
  needs an instruction-tuned model, so train the red-line lenses on the model
  actually used (for example `gemma-3-4b-it`). Do not assume a base-model pack
  transfers; test it.
- Train with HatCat's `scripts/train_full_lens_pack.py`, scoped to the new
  concepts placed under existing parents (Deception, Manipulation, Sabotage), so
  HAT's hierarchy still screens them.
- Validate on held-out examples before running in the world.

## Watch profile

[../profiles/redlines.txt](../profiles/redlines.txt) lists the new concepts and
the existing AI-risk parents they sit under.
