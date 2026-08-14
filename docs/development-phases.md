# Revised development phases

Each phase ends with tests, exact status/diff, unresolved decisions, and explicit user approval. Work stops
at every checkpoint. A phase number does not itself authorize service access or external mutations.

## Phase 0 — policy baseline, architecture, skeleton and invariants (complete)

Established platform-neutral identity/classification boundaries, provenance, canonical matching invariants,
write safety, data governance, provider ports, and synthetic offline safety tests.

## Phase 1 — configuration, runtime layout, SQLite persistence, foundational CLI and backup/restore

Adds strict versioned configuration/taxonomy schemas and a draft specification envelope; lazy per-user
runtime paths; SQLite migrations and basic Phase 0 domain round trips; transactionally synchronized taxonomy
state; foundational local CLI commands; and proportionate backup/restore. Synthetic data only.

## Phase 2 — Spotify PKCE and read-only adapter

Reverify current policy/API behavior; implement Authorization Code with PKCE, protected credential storage,
minimum read scopes, redaction, pagination, rate/quota handling, and fake-client tests. No writes.

## Phase 3 — SOURCE/INBOX ingestion plus canonical catalogue/matching workflow

Read configured owned/collaborative SOURCE playlists, Liked Songs, and a Shazam-style INBOX; persist platform
mappings, snapshots/cursors and processing state; and implement conservative matching queues with user
confirmation, rejection, and unlink. Sources/inboxes remain immutable.

## Phase 4 — finalize V1 taxonomy specifications and characteristic vocabulary

Finalize proportional specifications for the 15 broad Genres and the subjective Vibe/Activity playlists,
then review the characteristic vocabulary required for classification and research sufficiency.

## Phase 5 — independent research providers, caching, provenance and sufficiency

Add progressive independent providers and cache normalized provenance-aware evidence. Evaluate sufficiency
against the Phase 4 specifications/vocabulary before targeted research.

## Phase 6 — derived characteristics and deterministic rules

Implement versioned evidence-to-characteristic logic and deterministic broad-genre/activity rules. Generated
outputs remain proposals; tuning values remain configurable until real calibration.

## Phase 7 — review, overrides and calibration workflow

Implement proposal review, explicit include/exclude decisions, supersession, effective assignments, and
deliberately selected calibration examples before Codex classification exists.

## Phase 8 — Codex batch classification

Implement supervised, validated, resumable file export/import behind the classifier port without an embedded
model API or frozen arbitrary batch defaults.

## Phase 9 — desired state and dry-run synchronization planner

Build effective desired state and addition-only diffs using a fake platform. State/digest/snapshot changes
authoritatively invalidate approvals. Prove dry-run performs zero writes.

## Phase 10 — managed-playlist provisioning pilot

Reverify API/policy and create one approved empty managed playlist with reliable ambiguous-creation
reconciliation selected during the phase; do not mandate a user-visible operational marker in advance.

## Phase 11 — guarded addition-only population

Add approved missing items with configurable bounded batches, deduplication, 429/quota/partial-failure and
ambiguous-result reconciliation, verification, and journaling. No destructive mutation.

## Phase 12 — periodic inbox processing and V1 hardening

Provide the manually invoked end-to-end workflow with resumability, history, backup/recovery, and explicit
review/approval boundaries. Leave Liked Songs and Shazam entries intact.

## Future, separately approved

Calibration-based auto-approval, alternative platforms/classifiers, richer UI, scheduling, taxonomy migration
tools, removals, and a personal trained model only if later justified and permitted.
