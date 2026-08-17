# Revised development phases

Each phase ends with tests, exact status/diff, unresolved decisions, and explicit user approval. Work stops
at every checkpoint. A phase number does not itself authorize service access or external mutations.

Current roadmap status: **PHASES 0–6 COMPLETE. PHASE 7 NEXT / NOT STARTED.**

## Phase 0 — policy baseline, architecture, skeleton and invariants (complete)

Established platform-neutral identity/classification boundaries, provenance, canonical matching invariants,
write safety, data governance, provider ports, and synthetic offline safety tests.

## Phase 1 — configuration, runtime layout, SQLite persistence, foundational CLI and backup/restore (complete)

Adds strict versioned configuration/taxonomy schemas and a draft specification envelope; lazy per-user
runtime paths; SQLite migrations and basic Phase 0 domain round trips; transactionally synchronized taxonomy
state; foundational local CLI commands; and proportionate backup/restore. Synthetic data only.

## Phase 2 — Spotify PKCE and read-only adapter (complete)

Reverify current policy/API behavior; implement Authorization Code with PKCE, protected credential storage,
minimum read scopes, redaction, pagination, rate/quota handling, and fake-client tests. No writes.

## Phase 3 — SOURCE/INBOX ingestion plus canonical catalogue/matching workflow (complete)

Implemented and live-validated explicit SOURCE registration, the singleton Saved Tracks/Liked Songs INBOX,
and one explicitly selected playlist-backed Shazam INBOX. Stable `account_id` binding, ephemeral discovery,
completed-publication gating, immutable SOURCE/INBOX behavior, account-scoped catalogue adoption, and local
review were verified. The final baseline has 806 published occurrences, 744 published provider identities,
740 portable canonical tracks, 744 active account-scoped links, four explicit existing-canonical decisions,
and no remaining review-required identity. Spotify access remained read-only; no writer exists.

## Phase 4 — finalize V1 taxonomy specifications and characteristic vocabulary (complete)

The explicitly human-reviewed controlled vocabulary, exact 36-file semantic manifest, centralized overlap
guidance, 51-target registry, Greek/Foreign Genre routing policy, strict schemas, deterministic bundle
digest, semantic validation, and offline tests are complete. All 15 Genre, 10 Vibe, and 11 Activity
specifications are `reviewed`. No runtime routing, definition synchronization, research, or classification
execution was introduced during Phase 4.

## Phase 5 — independent research providers, caching, provenance, sufficiency

The implementation adds conservative MusicBrainz and Wikidata read adapters, exact-recording subject
fingerprints, compact provider assertions, normalized evidence, provenance, deterministic positive/negative
caching, bounded retries, and explicit per-evidence-need sufficiency states. Its authorized local checkpoint
validated successful research, provenance, conservative probable matching, cache reuse, and preservation of
ambiguous/contradictory evidence for review. Provider calls remain explicit bounded `--network` actions;
ordinary inspection and tests remain offline.

## Phase 6 — derived characteristics and deterministic rules (complete)

Implements versioned, offline evidence-to-characteristic derivation and generic reviewed-specification
evaluation. Missing, stale, ambiguous, contextual, or contradictory evidence remains unknown or
review-required; the result is an explainable proposal, never an assignment. No provider refresh, Spotify
access, desired-state construction, output-target routing, or playlist write occurs here. Phase 7 owns
human review, overrides, and calibration. Its successful local checkpoint confirmed repeatable abstention
when evidence was insufficient; no provider or Spotify access occurred.

## Phase 7 — review, overrides, calibration

Implement proposal review, explicit include/exclude decisions, supersession, effective assignments, and
deliberately selected calibration examples before Codex classification exists.

## Phase 8 — Codex supervised file-exchange classification

Implement supervised, validated, resumable file export/import behind the classifier port without an embedded
model API or frozen arbitrary batch defaults.

## Phase 9 — desired state / dry-run synchronization

Build effective desired state and addition-only diffs using a fake platform. State/digest/snapshot changes
authoritatively invalidate approvals. Prove dry-run performs zero writes.

## Phase 10 — managed-playlist provisioning pilot

Reverify API/policy and create one approved empty managed playlist with reliable ambiguous-creation
reconciliation selected during the phase; do not mandate a user-visible operational marker in advance.

## Phase 11 — guarded addition-only population

Add approved missing items with configurable bounded batches, deduplication, 429/quota/partial-failure and
ambiguous-result reconciliation, verification, and journaling. No destructive mutation.

## Phase 12 — periodic inbox processing / V1 hardening

Provide the manually invoked end-to-end workflow with resumability, history, backup/recovery, and explicit
review/approval boundaries. Leave Liked Songs and Shazam entries intact.

## Future, separately approved

Calibration-based auto-approval, alternative platforms/classifiers, richer UI, scheduling, taxonomy migration
tools, removals, and a personal trained model only if later justified and permitted.
