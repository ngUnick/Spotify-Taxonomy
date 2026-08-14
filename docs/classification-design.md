# Classification design baseline

## Traceability model

The system keeps five different provenance/origin axes because they answer different questions:

| Concept | Question answered | Representative values |
| --- | --- | --- |
| Identity provider | Where did this track identity/mapping come from? | Spotify, MusicBrainz, user import |
| Research source | Where did this externally asserted information come from? | Last.fm, Wikipedia, publication, user |
| Evidence kind | What epistemic kind of information is this? | sourced fact, community tag, editorial description |
| Characteristic origin | Who/what derived this reusable characteristic? | deterministic rule, Codex interpretation, user |
| Proposal origin | Who/what proposed this playlist decision? | genre mapping rule, characteristic rule, Codex |
| Decision origin | How did the effective human/operational decision arise? | approval, override, explicit decision |

Codex and deterministic rules are not external research sources. Spotify identity is not research
evidence. This permits a clean trace: identity → facts/tags → characteristics → proposals → decisions.

## Evidence and characteristics

Research stores normalized facts, tags, or compact descriptions plus source identifiers/URLs where useful.
It does not archive whole pages. Contradictory claims remain separate records and reduce downstream
confidence. Insufficient information produces a review state rather than invented certainty.

A derived characteristic references its supporting evidence IDs, confidence, derivation version, origin,
and a compact justification. Characteristics precede final playlist proposals. Examples may later include
energy, rhythmic drive, intensity, atmosphere, valence-like affect, vocal prominence, or instrumental
character; this list is illustrative and is not a finalized taxonomy.

## Genre, vibe, and activity

Broad genre proposals should prefer versioned deterministic mappings from reliable independent genre/tag
evidence. Multi-parent mappings are allowed (for example, a later reviewed mapping could map electropop to
both Pop and Electronic). Codex should resolve ambiguity, weak evidence, and subjective boundaries rather
than spend reasoning on obvious parent relationships.

Vibes and activities consume playlist specifications, evidence, characteristics, and selected user-reviewed
examples. Activity logic may reuse characteristics and vibe-relevant concepts through explicit scoring or
rules. It does not need to independently rediscover energy or atmosphere for each playlist.

## Proposal output

Every proposal contains a logical playlist ID, include/exclude/abstain decision, confidence, proposal origin,
taxonomy and specification versions, review status, relevant evidence/characteristic IDs, and a concise
justification summary. The current domain contract caps that summary at 280 characters. It intentionally
does not request or persist extensive model reasoning.

Generated proposals are suggestions, not truth. A user may approve or reject them, add an unproposed
assignment, explicitly exclude a track, or revise a prior decision. Precedence is:

```text
USER_OVERRIDE / USER_EXPLICIT
    > USER_APPROVAL
    > generated proposal
```

Conflicting equal-confidence generated proposals resolve to abstain/needs-review. An auto-approval threshold
is deliberately not selected in Phase 0; real calibration results must inform it.

## Calibration examples

Reviewed approvals, rejections, positive additions, and negative overrides can be explicitly selected as
positive/negative examples for a logical playlist. Ordinary historical predictions are not automatically
training examples. Example sets are versioned inputs to later Codex batches, not model training.

## Canonical matching invariant

Normalized title/artist equality produces an ambiguous candidate, never an automatic merge. The catalogue
supports multiple recordings and releases—including live, acoustic, remix, remaster, edit, explicit/clean,
or otherwise ambiguous variants. Strong provider IDs can confirm two mappings from the same provider;
cross-provider matches need corroboration or user confirmation. Match confidence, basis, state, rejections,
and later confirmations remain auditable.

## Codex boundary

V1 uses Codex for batch reasoning behind the provider-neutral `BatchClassifier` port. Inputs contain only
deliberately selected identity, normalized independent evidence, specifications, and calibration examples—
never credentials or raw account/API dumps. Output is validated before persistence. Classification cannot
receive a Spotify mutation capability and completion of classification cannot trigger a write.
