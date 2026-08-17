# Phase 4 taxonomy specifications

The completed Phase 4 bundle contains one explicitly human-reviewed specification for each of the 36 stable V1 logical
playlist IDs: 15 Genre, 10 Vibe, and 11 Activity. `config/specifications/manifest.yaml` is the authoritative
one-to-one map, while `config/specifications/overlaps.yaml` centralizes ambiguous-group guidance. The
manifest also references a separate output-target registry and Genre routing policy; neither duplicates a
semantic specification.

Each specification states its purpose and dimension basis, then separates strong, supporting, negative,
and rare hard-exclusion indicators. Conditions reference only controlled characteristic IDs and typed
operators. Each file also includes ambiguity, exact-version, vocal/instrumental, and overlap guidance.
Material uncertainty produces abstention. Membership remains many-to-many, and no specification requires
an assignment within any dimension.

## Semantic playlists and concrete output targets

```text
36 semantic concepts
→ independent evidence and reusable characteristics
→ semantic classification
→ Greek/Foreign routing for Genre
→ 51 concrete output targets
```

This is a definition flow, not an implemented runtime pipeline; research and execution begin only in later
separately authorized phases.

The semantic taxonomy remains 36 concepts and 36 specifications. `config/output-targets.yaml` defines 51
stable future targets: Greek and Foreign variants for each of the 15 Genre concepts, plus one unsplit target
for each Vibe and Activity. `config/genre-variant-routing.yaml` applies only after musical Genre
qualification. Predominantly Greek lyrics route Greek; predominantly non-Greek lyrics route Foreign; and
instrumental/non-lexical recordings use regional context before primary project/scene association.

Foreign includes every non-Greek language and context, not only English. Balanced, unknown, mixed-context,
or co-equal cases remain unresolved for later review. Routing is scoped to the exact canonical recording,
so alternate-language, instrumental, remix, and bilingual versions may differ. Display names are metadata
only and confer no mutation authority.

## Dimension bases

- Genre specifications use independently supported musical style or work-context evidence. They cover broad
  traditions without treating string normalization or a single weak tag as proof.
- Vibe specifications use affect, energy, atmosphere, sonic character, and separately represented lyrical
  themes. Their overlapping concepts are axes rather than a hierarchy.
- Activity specifications evaluate functional suitability independently. General and specialized contexts
  do not inherit membership from one another.

## Central overlap guidance

The required overlap records cover Chill/Instrumental; the four positive-activation vibes; Dark/Emotional;
Driving/Night Driving/Road Trip; Gym/Running; Studying/Coding/Background; Party/Hype/Euphoric; three broad
style intersections; Folk/Country; R&B/Pop/Hip-Hop; Latin/World; and Soundtracks/other styles.

Notable boundaries include independent Chill qualification before the nested instrumental subset; sparse
non-lexical vocals without ordinary lyrical lead; separate gym intensity and running continuity axes;
no automatic Latin-to-World relationship; and work-specific score/theme evidence rather than ordinary
soundtrack-compilation appearance. Both sides of an overlap may be valid when independently supported.

Folk/Acoustic has two strong routes: documented Folk lineage, or fully/mostly acoustic balance together
with the reusable `arrangement.substantive_acoustic_idiom` judgment. Acoustic balance alone is not a route.
Reggae/Ska retains tradition-only qualification while secondary evidence now represents both Reggae/dub
and Ska performance traits. Soundtracks/Scores deliberately uses `work_context` as its dimension basis;
compilation appearance is negative only when no work-specific function is supported.

## Validation and review status

`spotify-taxonomy config validate` loads the entire bundle offline, restricts references to source-controlled
configuration paths, verifies versions/statuses and exact ID coverage, resolves characteristic and overlap
references, enforces all 51 output targets and the closed routing decision table, type-checks operators and
values, and emits a deterministic bundle digest covering both new documents. It requires no
database, Spotify reader, credential, or network service.

All 36 specifications, the manifest, and the controlled vocabulary are `reviewed` following explicit human
approval. Phase 4 is complete. These documents remain source definitions only: Phase 5 has not started, and
no research provider, characteristic derivation, classifier, runtime routing executor, desired-state builder,
or Spotify writer is implied by review status.
