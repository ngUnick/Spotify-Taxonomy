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

## Backup and restore

Backups use SQLite's consistent backup API and a small JSON manifest with schema/taxonomy/specification
versions and a SHA-256 digest. Verification checks the digest, SQLite integrity, and supported schema.
Restore verifies first, protects an existing database with a pre-restore backup, copies through the state
directory, replaces atomically, and verifies again. Retention touches only recognized application backups
inside the resolved backup directory.
