# Spotify Playlist Taxonomy

This repository currently contains **Phase 0 only**: policy, architecture, safety boundaries,
small domain contracts, and synthetic invariant tests for a personal music-classification and
playlist-synchronization project.

Nothing here authenticates with Spotify, researches or classifies real tracks, creates playlists,
or performs network calls. The future system will keep classification platform-portable and will
permit writes only to explicitly registered managed playlists through a guarded, preview-first path.

## Phase 0 validation

From the repository root with Python 3.13 or later:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

The tests use invented track and playlist data. They require no credentials, network access, or
third-party dependencies.

## Documentation map

- `docs/architecture.md` — layers, dependencies, data flow, and V1 boundaries.
- `docs/spotify-policy-baseline.md` — dated API/policy baseline and accepted policy risk.
- `docs/data-governance.md` — retention, privacy, and provenance rules.
- `docs/safety-model.md` — source/inbox/managed roles and guarded mutation path.
- `docs/classification-design.md` — evidence, characteristics, proposals, review, and batches.
- `docs/development-phases.md` — revised roadmap; only Phase 0 has been executed.
