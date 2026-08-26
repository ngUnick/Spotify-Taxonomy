# Revised development phases

Each phase ends with tests, exact status/diff, unresolved decisions, and explicit user approval. Work stops
at every checkpoint. A phase number does not itself authorize service access or external mutations.

Current roadmap status: **PHASES 0–12 / V1 IMPLEMENTED.** Schema 19 is current. V1 has completed a
supervised end-to-end Spotify population checkpoint: explicit human review produces desired membership,
then exact managed-target verification and guarded one-track addition provide the only population path.
Current execution focuses on small human-reviewed batches and fail-closed population, not broad matching or
external-provider research expansion.

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

## Phase 7 — review, overrides, calibration (complete)

Adds local, auditable review history, explicit include/exclude/abstain decisions, reversible clears, and
an effective state where an explicit user decision outranks a generated proposal. Calibration is
observational reporting only: it never tunes rules or taxonomy. No desired state, routing, or Spotify write
is created. The completed local checkpoint verified deterministic, conflict-focused queue behavior without
creating any user decision. Phase 8 is the supervised generated-classification boundary.

## Phase 8 — Codex supervised file-exchange classification

Implement supervised, validated, resumable file export/import behind the classifier port without an embedded
model API or frozen arbitrary batch defaults.

Phase 8 implements a complete supervised file exchange: ResearchEvidence → DerivedCharacteristic →
deterministic ClassificationProposal → validated outbound batch → manual Codex processing → untrusted
inbound result → strict validation and transactional import → Codex-generated ClassificationProposal →
Phase 7 human review/effective state. Codex/OpenAI is not an embedded runtime dependency and the application
never sends a batch externally. Transfers are manual, outbound context is minimized, and title/artist remain
excluded, along with Spotify/account/playlist/source identifiers and raw provider responses. Inbound result
files are untrusted until strict validation succeeds; no chain-of-thought is requested or persisted. Imports
are transactional and auditable; deterministic and Codex-generated proposals may coexist, generated-source
disagreements remain reviewable, identical result replay is idempotent, and explicit user decisions outrank
all generated proposals. Phase 8 performs no Spotify write. Later phases consume only the effective reviewed
state; they do not give generated proposals direct playlist authority.

## Phase 9 — desired state / dry-run synchronization

Phase 9 derives concrete desired membership from effective reviewed state only: 36 semantic concepts route to
51 configured targets. Greek/Foreign routing applies strictly after included Genre semantics through the
reviewed routing policy; unresolved routing produces no guessed target. Schema 19 also supports bounded,
explicit human Greek/Foreign routing decisions; this routing is separate from, and cannot create, Genre
classification. It is a local dry-run planner: a
target without an authoritative concrete managed mapping is unprovisioned, authoritative absent membership
is `would_add`, and present desired membership is `no_op`. Present-but-not-desired membership is informational
only; no removal is planned or executable. Phase 9 performs zero Spotify writes. Phase 10 owns managed
playlist provisioning and authoritative managed-target snapshots. Its local read-only checkpoint confirmed
that an all-abstained, agreement-only effective state produces zero desired memberships and zero dry-run
actions without introducing a review decision or any Spotify write.

## Phase 10 — managed-playlist provisioning pilot

Phase 10 provisions at most one explicitly selected concrete output target at a time. A durable local
registration binds its stable `output_target_id` to the exact Spotify playlist identity and active account
binding; names and descriptions are never used for adoption or recovery. Creation sends the best-effort
preference `public=false`, requires non-collaborative provider state, and uses exactly the informational
description `Managed by Spotify Taxonomy.`
The opt-in authorization command requests only `playlist-modify-private`; no public modification scope is
requested. Preview is local and write-free. Apply requires the exact target ID as confirmation, verifies the
bound account before creation, creates the empty playlist, then records its returned identity. A repeated
active target is idempotent. Any ambiguous remote/local partial success is `recovery_required` and cannot be
automatically retried, adopted, recreated, or deleted. This phase has no population, removal, rename, edit,
or generic playlist-mutation capability. Phase 9 can recognize an active registration but remains
`out_of_scope` until a future authoritative managed-membership read exists.

An unknown `recovery_required` result remains blocked until a human explicitly runs `managed recovery
abandon <target-id> ABANDON-UNKNOWN:<target-id>`. That local-only action preserves the unresolved attempt
as `abandoned_unknown`; it neither asserts remote failure nor adopts/searches for a playlist. It performs no
Spotify write, deletion, or cleanup, and it never retries automatically. Normal provisioning may only be
previewed or later applied again through its separate existing safeguards.

The provider-observed `public` flag is informational only: it is neither ownership evidence nor an
authoritative proxy for profile publication or Spotify-client access privacy. Creation still sends
`public=false` and `collaborative=false` explicitly. Exact-ID verification requires matching identity,
owner/account binding, and `collaborative=false`; it retains `public` as bounded diagnostic metadata.
A collaborative provider response remains fail-closed. Earlier active registrations are policy-unconfirmed
until an exact-ID verification confirms the authoritative conditions.

`managed verify` reads only the exact durable registration identity and reports bounded public/collaboration
metadata. No policy-repair command is exposed: the historical repair journal remains for audit only. Phase 9
recognizes only policy-confirmed targets, without requiring any specific provider `public` value.

## Phase 11 — guarded addition-only population

Append only one explicitly selected canonical track to one exact policy-confirmed managed target per
invocation. Apply recomputes desired state, resolves one existing Spotify mapping without title/artist
matching, validates the exact destination, and reads full membership before adding. `ADD:<target>:<canonical>`
is required. Existing or explicitly relinked membership is a no-op. Ambiguous outcomes require read-only
recovery verification; no retry, removal, replacement, reorder, bulk/background population, or Phase 12
synchronization exists.

## Phase 12 — periodic inbox processing / V1 hardening

`cycle status` is a local-only, deterministic operational report. It is safe to schedule externally because
it performs no provider I/O or mutation, but V1 does not schedule ingestion: selected source scans, research
with `--network`, Codex exchange, review, provisioning, and one-track population remain explicit operator
actions. Its incomplete-ingestion category reports only incomplete scans for active sources; disabled-source
scan history remains persisted for audit without appearing as current actionable work. Logout removes protected
credentials only; unbind requires sources disabled and preserves history.

For canonical match review, use `catalogue match list --state ambiguous --limit 25`, inspect one row with
`catalogue match show CANDIDATE_ID`, then explicitly choose either `confirm-existing` for that candidate or
`confirm-new` using the displayed open-peer candidate ID. Confirmation resolves that identity and supersedes
its open peers; no bulk matching authority exists.

Phase 15 adds only nullable provider-derived duration milliseconds and ISRC to observed Spotify identities.
The local-only `catalogue match evidence-status` command reports aggregate evidence counters for unresolved
link-existing candidates and never exposes identities or makes decisions. Existing approved source scans refresh
these fields for observed identities. `catalogue match evidence-backfill` is a separately confirmed, sequential
Spotify-read maintenance operation: it selects only current ambiguous `link_existing` identities and existing
account-scoped identities linked to their proposed canonicals, then retains only duration and ISRC. It reports
aggregates only, never scans sources, makes no matching decision, and uses a single-track read rather than a
deprecated multi-track lookup. A stored duration marks a completed request, so an optional absent ISRC is not
retried indefinitely. Run `db init` first when upgrading an existing runtime database so its explicit migration
mechanism applies schema 15.
`catalogue match evidence-list --category CATEGORY --limit 1..100` is a local, read-only operator review
surface. It emits one deterministic row per linked comparison identity (so multiple comparisons are never
collapsed) for `same-isrc-duration-gt-5s`, `same-isrc-duration-gt-2s`,
`different-isrc-duration-le-1s`, `different-isrc-exact-duration`, or `same-isrc-exact-duration`. It makes
no provider call or local state change.
`catalogue match deterministic preview` evaluates the fixed `canonical-tier-a.v1` policy locally only:
normalized title/artists context, same ISRC, duration delta at most 500ms, no explicit version conflict, and
all account-scoped comparison identities agreeing. It is preview-only: no link, canonical, candidate, or
source mutation path is provided.
`catalogue match deterministic apply --limit N` is the only mutation entry point for the fixed policy. It
requires confirmation (or `--yes`), rechecks each selected candidate in the existing confirmation
transaction, and records compact local provenance under schema 16. `deterministic history` is local,
read-only, and never contacts a provider.
Spotify track ingestion does not manufacture `version_label`; evidence status therefore falls back to the
existing deterministic title-marker helper (`live`, `acoustic`, `remix`, `remaster`, `edit`, `explicit`,
`clean`, and `instrumental`) for reporting only. It distinguishes compatible explicit cues, conflicts,
neither-side cues, and one-sided/uncertain cues without changing candidate-generation protection.
V1 population has been demonstrated under its guarded path. Practical operation now uses small, explicit
human-reviewed batches, followed by desired-state inspection and one target + one canonical population applies.
Ambiguous or unresolved provider mappings remain blocked; no matching-safety relaxation, removal, replacement,
or bulk/background execution is authorized.

## Future, separately approved

Calibration-based auto-approval, alternative platforms/classifiers, richer UI, scheduling, taxonomy migration
tools, removals, and a personal trained model only if later justified and permitted.
