# Phase 1 persistence and runtime foundation

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

It deliberately has no source-scan, research-job, Codex-batch, processing-run, desired-state, sync-plan,
or operation-journal tables. Effective assignments and mutation authorization remain derived runtime data.

## Backup and restore

Backups use SQLite's consistent backup API and a small JSON manifest with schema/taxonomy/specification
versions and a SHA-256 digest. Verification checks the digest, SQLite integrity, and supported schema.
Restore verifies first, protects an existing database with a pre-restore backup, copies through the state
directory, replaces atomically, and verifies again. Retention touches only recognized application backups
inside the resolved backup directory.
