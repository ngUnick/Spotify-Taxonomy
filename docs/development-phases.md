# Revised development phases

Each phase ends with tests, an exact status/diff, unresolved decisions, and explicit user approval. Work must
stop at every checkpoint. Phase numbers describe architecture increments, not permission to contact services.

## Phase 0 — policy baseline, architecture, skeleton, invariants (complete)

Record current Spotify risk/API baseline and the user's risk decision; define data governance, provenance,
classification, track matching, and write-safety architecture; create dependency-free contracts and synthetic
invariant tests. No database, OAuth, network client, real data, classification, or write behavior.

## Phase 1 — configuration, schemas, persistent local state

Define versioned taxonomy/specification and batch schemas; establish `%LOCALAPPDATA%` layout, SQLite schema
and migrations, repository/runtime configuration boundaries, provenance constraints, decision precedence,
and backup/restore. Use synthetic fixtures only.

## Phase 2 — Spotify OAuth and read-only client

Reverify policy/API; implement Authorization Code with PKCE and protected credential storage; minimum scopes,
redaction, pagination, rate/quota handling, and fake-client tests. No real connection until a separate
credential/setup checkpoint and no writes.

## Phase 3 — read-only ingestion and canonical catalogue

Ingest configured owned/collaborative SOURCE, Liked Songs, and Shazam-style INBOX identities; create platform
mappings, snapshots/cursors, processing state, and ambiguity-aware matching/review. Support legitimate manual
imports for inaccessible third-party playlists. Never modify sources or merge solely on normalized strings.

## Phase 4 — independent research providers and caching

Add provider adapters progressively, terms-aware normalized evidence, source references, cache/freshness and
research status. Query cheap structured sources first; perform targeted web research only when insufficient.
No real catalogue research until separately approved.

## Phase 5 — playlist specifications and broad-genre rules

Write/edit the 15 broad Genre plus provisional Vibe/Activity specifications. Create reviewed deterministic
tag-to-parent mappings and evidence-to-characteristic rules with versioning. Do not finalize subjective
definitions without user review.

## Phase 6 — Codex batch classification and review

Implement deterministic schemas, fingerprints, batch/result validation, resumability, retry of unresolved
items, compact evidence-linked summaries, confidence/review states, and user decisions/calibration examples.
Keep Codex behind the classifier port and do not train a model. Calibrate rather than preselect auto-approval
thresholds.

## Phase 7 — dry-run synchronization planner

Build effective desired state and addition-only diffs using a fake platform. Add operation IDs, approval
artifacts, reconciliation, and proof that dry-run performs zero writes.

## Phase 8 — managed-playlist provisioning

Reverify API/policy. Add guarded creation only, stable-ID registration, live ownership verification,
configurable visibility, approval, verification, and recovery tests before any real creation.

## Phase 9 — guarded addition-only population

Add bounded approved additions to registered managed playlists; deduplicate, batch, handle 429/quota/partial
failure/ambiguous results, verify, and journal. No removal/replacement/reordering/detail/visibility mutation.

## Phase 10 — periodic inbox processing

Provide a manually invoked “process new songs” workflow using durable cursors/status, research, review,
preview, guarded addition, and verification. Leave Liked Songs and Shazam entries intact.

## Future, separately approved

Calibration improvements; alternative hosted/local/rules/ML classifiers; a personal trained multi-label
model only if later justified and permitted; alternate music platforms; richer UI; scheduling; removals;
taxonomy migration tooling; and other automation. None is part of V1 approval.
