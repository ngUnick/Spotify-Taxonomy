# Spotify Playlist Taxonomy

This repository contains the Phase 0 architecture plus the Phase 1 local-state foundation for a
personal, reviewable music-classification and playlist-synchronization project.

Phase 1 provides strict configuration and taxonomy validation, lazy per-user runtime paths, SQLite
migrations and foundational persistence, a local Typer CLI, and integrity-checked backup/restore.
It does **not** authenticate with Spotify, contact network services, ingest real music, run research or
classification, create synchronization plans, or modify playlists.

## Setup and validation

Python 3.13 or later and `uv` are required.

```powershell
uv sync --locked
uv run spotify-taxonomy config validate
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```

All ordinary tests are offline and use invented data in temporary runtime directories.

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
