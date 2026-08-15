# Spotify Playlist Taxonomy

This repository contains the Phase 0 architecture, Phase 1 local-state foundation, and Phase 2 offline
Spotify PKCE/read-only adapter for a personal, reviewable music-classification and synchronization project.

Phase 2 adds least-privilege PKCE, protected Windows refresh-token storage, bounded read retries, and a
platform-neutral Spotify reader. The offline checkpoint has no real authorization or account access.
It does **not** ingest real music, run research/classification, create synchronization plans, or modify
Spotify. No writer is implemented.

## Setup and validation

Python 3.13 or later and `uv` are required.

```powershell
uv sync --locked
uv run spotify-taxonomy config validate
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```

All ordinary tests are offline and use invented data in temporary runtime directories.

Spotify dashboard and PKCE setup are documented in `docs/spotify-setup.md`. Do not perform a real login or
`spotify check` without explicit checkpoint approval.

## Runtime paths

The V1 runtime-root precedence is exactly:

```text
--data-dir
> SPOTIFY_TAXONOMY_DATA_DIR
> %LOCALAPPDATA%\SpotifyPlaylistTaxonomy
```

Directories are created lazily. `db init` creates only the runtime root and `state/`; backup and file
logging directories appear only when those features are used. Batch/export paths are defined for future
phases but are not created by Phase 1.

## Foundational commands

```text
spotify-taxonomy status
spotify-taxonomy runtime paths
spotify-taxonomy config validate
spotify-taxonomy db init
spotify-taxonomy db status
spotify-taxonomy backup create
spotify-taxonomy backup verify BACKUP
spotify-taxonomy backup restore BACKUP
spotify-taxonomy spotify auth status
spotify-taxonomy spotify auth login
spotify-taxonomy spotify auth logout
spotify-taxonomy spotify check
```

`db init` validates the source-controlled taxonomy, migrates SQLite, and transactionally synchronizes
the taxonomy version, digest, and logical-playlist metadata. An unchanged rerun is idempotent. Unsafe or
unversioned taxonomy changes fail rather than silently rewriting state.

## Documentation

- `docs/architecture.md` — component boundaries and data flow.
- `docs/persistence.md` — Phase 1 schema, migrations, runtime paths, and backup behavior.
- `docs/data-governance.md` — retention, privacy, and provenance.
- `docs/safety-model.md` — playlist roles and guarded mutation design.
- `docs/classification-design.md` — evidence, characteristics, proposals, and review.
- `docs/spotify-policy-baseline.md` — dated Spotify API/policy risk record.
- `docs/spotify-setup.md` — least-privilege dashboard, PKCE, credential, and revocation guidance.
