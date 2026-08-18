# Phase 1 persistence foundation and Phase 3 additions

## Runtime layout

The runtime root is selected only by CLI `--data-dir`, `SPOTIFY_TAXONOMY_DATA_DIR`, or the
`%LOCALAPPDATA%\SpotifyPlaylistTaxonomy` default. Application YAML cannot override it.

Paths are lazy: database initialization creates `state/`; backup and file logging create their own
directories on first use. Phase 1 does not create batch or export directories.

## Migrations

Numbered packaged SQL migrations are checksummed and recorded in `schema_migrations`. Versions must be
contiguous. Each unapplied script and its migration record execute in one transaction. The application
refuses changed migration checksums and databases newer than it supports, enables foreign keys on every
connection, and runs SQLite integrity validation after migration.

## Taxonomy synchronization

The YAML document carries `schema_version` and `taxonomy_version`; playlist entries carry logical ID,
dimension, display name, enabled state, and specification status. `db init` validates before creating
runtime state, then stores the version/digest and one immutable revision row per logical playlist.

An unchanged version/digest is a no-op. Content changes under the same version fail. A new version may
add logical IDs or revise display/enabled/specification-status metadata, but it cannot omit established
IDs or change their dimensions. These conservative rules can be expanded later through explicit migrations.

## Persisted concepts

Phase 1 stores taxonomy revisions; canonical tracks and ordered artists; platform track identities;
explicit canonical/identity links; match assessments; platform playlist registrations; research evidence;
derived characteristics and evidence references; proposals and their references; user decisions; and
explicit calibration selections.

Phase 3 migration 4 adds only account bindings, explicit ingestion sources, registered-playlist observations,
staged scans and occurrences, current source publications, identity-ingestion state, conservative match
candidates, and link-event history. It does not add research jobs, Codex batches, desired state, sync plans,
or operation journals.

Composite keys and foreign keys enforce registration/binding, source/registration, scan/source,
publication/scan/source, and account identity-state relationships. Lifecycle conditions that cannot be
expressed by a foreign key (notably terminal enumeration and completed publication) are revalidated in one
write transaction. A publication can never point at another source's scan.

An account binding is keyed by Spotify `account_id`; changed current-user IDs update verification metadata.
Sources, scans, and publications remain attached to their original binding and cannot be adopted by another
account. Unbinding is denied while any source remains active.

Canonical tracks and provider identities remain global, but `account_canonical_identity_links`,
`account_track_identity_state`, match candidates, and link events hold account-specific adoption and user
decisions. Candidate identity uses a non-null content-derived key, including the create-new alternative, so
reconciliation is idempotent. Confirmation atomically confirms one candidate and supersedes incompatible
reviewable peers; failures roll back the link, event, candidate decision, and any new canonical row.

`source_scan_entries` uses `(scan_id, source_position)` as its primary occurrence identity. A scan becomes
authoritative only when `source_publications` atomically points at its completed, consistency-checked result.
The scan content digest stores an explicit `digest_version` and excludes page-relative boundaries.
Only completed current publications from active sources are matching inputs. Staged state in pending,
running, failed, stale, or abandoned scans remains historical and non-authoritative.

## Phase 4 non-persistence boundary

The Phase 4 characteristic vocabulary, specification manifest, overlap guidance, 36 semantic playlist
specifications, 51-target registry, and Genre variant-routing policy are source-controlled reviewed documents.
Phase 4 validation reads them without opening or modifying SQLite. The existing `specification_versions`
table does not imply automatic synchronization; runtime consumption and synchronization remain deferred to
the later phase that needs these definitions. Phase 4 adds no migration.

The existing managed-playlist registration shape contains only `logical_playlist_id`; it is intentionally
unchanged here. The later managed-playlist/desired-state phase must migrate that boundary to bind a managed
registration to stable `output_target_id`, allowing Greek and Foreign targets to share one semantic Genre
parent without relying on display names.

## Phase 10 managed-target registration

Migration 9 adds `managed_target_registrations`, keyed by the stable concrete `output_target_id`. It stores
only the provider, active account-binding reference, exact returned provider playlist identity, lifecycle
state, and creation provenance. `active` is the only durable usable state. `provisioning` and
`recovery_required` deliberately block automatic retries, name/description matching, adoption, recreation,
and cleanup after uncertain creation. It does not store memberships or make a playlist writable for track
population; those are separate future concerns.

Migration 10 adds `managed_target_abandoned_unknown_attempts` for a narrow explicit operator recovery
case. It archives only a `recovery_required` attempt with no known provider playlist identity, records that
its outcome is `abandoned_unknown`, and removes the live blocking row in one local transaction. This never
claims remote failure or success, searches/adopts a playlist, or performs provider I/O. The preserved history
allows a later separately confirmed provisioning attempt to receive a new registration identity.

Migration 11 adds `policy_confirmed` to live managed registrations. New active registrations require a
non-collaborative provider response. Exact-ID verification confirms the authoritative identity/account and
non-collaboration conditions; observed provider `public` remains informational rather than authoritative.
Existing rows remain unconfirmed rather than being retroactively interpreted from local intent.

Migration 12 adds a bounded historical policy-repair attempt journal. It preserves prior diagnostic outcomes
without retaining raw provider responses. No policy-repair operation is currently exposed.

Migration 13 adds only sanitized transport facts to that journal for future repair attempts: HTTP status,
whether dispatch/acknowledgement is known, and the exact-ID read-back publication/collaboration values. It
does not alter or infer facts for historical attempts, and stores no request URL, identifier, header, token,
or response body.

Migration 14 adds `managed_target_addition_attempts` for Phase 11's one-target/one-canonical addition
pilot. It records only local identity and registration references, confirmation, bounded dispatch/read-back
facts, lifecycle outcome, and timestamps. A committed `started` or `recovery_required` row blocks an
automatic retry after an uncertain provider addition. It stores no provider response, URI, snapshot, token,
or playlist-content copy.

## Phase 5 research state

Migration 5 adds `research_jobs`, `research_provider_attempts`, `research_provider_assertions`, and
`research_cache_entries`, and extends `research_evidence` with provider/query identity, subject fingerprint,
scope/match, typed claim, quality, normalization version, freshness, invalidation, and content digest.
Legacy evidence remains readable. Cache hits and retries are auditable, while contradiction groups and
sufficiency reports remain deterministic computed views. No raw provider response or playlist assignment is
stored.

## Backup and restore

Backups use SQLite's consistent backup API and a small JSON manifest with schema/taxonomy/specification
versions and a SHA-256 digest. Verification checks the digest, SQLite integrity, and supported schema.
Restore verifies first, protects an existing database with a pre-restore backup, copies through the state
directory, replaces atomically, and verifies again. Retention touches only recognized application backups
inside the resolved backup directory.
