# Spotify Playlist Taxonomy

Phases 0 through 3 are complete. The repository contains the safety architecture, local SQLite foundation,
Spotify PKCE/read-only adapter, and the live-validated SOURCE/INBOX ingestion and canonical-matching
workflow. Phase 4 has not started.

The completed Phase 3 runtime has one playlist SOURCE publication (48 occurrences), the singleton Saved
Tracks/Liked Songs INBOX publication (506 occurrences), and one explicitly selected playlist-backed Shazam
INBOX publication (252 occurrences). Their completed, consistency-checked publications contain 806
occurrences and 744 distinct provider identities. All 744 identities have active account-scoped canonical
links to 740 portable canonical tracks; four ambiguous duplicate cases were explicitly confirmed as links
to existing canonicals, and no review-required identity remains.

The stable Spotify `account_id` is the local binding boundary; playlist discovery remains ephemeral until a
stable ID is explicitly registered. Provider identities and canonical tracks are global/portable, while
adoption links, candidates, interventions, and decision history are account-scoped. Playlist ingestion uses
only `/playlists/{id}/items` and canonical nested `item`; the legacy nested-`track` fallback was removed after
live validation. Spotify remained read-only throughout Phase 3. No modify scope or Spotify writer exists.

The project does **not** yet run research/classification, finalize desired state, create synchronization
plans, or modify Spotify.

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
spotify-taxonomy spotify account status
spotify-taxonomy spotify account bind
spotify-taxonomy spotify account unbind
spotify-taxonomy spotify source discover
spotify-taxonomy spotify source register-playlist PLAYLIST_ID --role source
spotify-taxonomy spotify source register-liked
spotify-taxonomy spotify source list
spotify-taxonomy spotify ingest scan SOURCE_ID
spotify-taxonomy spotify ingest status
spotify-taxonomy catalogue status
spotify-taxonomy catalogue identity show IDENTITY_ID
spotify-taxonomy catalogue match list
spotify-taxonomy catalogue match show CANDIDATE_ID
spotify-taxonomy catalogue match confirm-existing CANDIDATE_ID CANONICAL_ID
spotify-taxonomy catalogue match confirm-new CANDIDATE_ID
spotify-taxonomy catalogue match reject CANDIDATE_ID
spotify-taxonomy catalogue link list
spotify-taxonomy catalogue link unlink IDENTITY_ID
```

Catalogue review commands are local and scoped to the active account binding. They deliberately show only
the concise title, ordered artists, version cue, candidate basis, and link state needed for an interactive
decision; routine logs and automated reports do not receive that metadata.

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
- `docs/project-handoff.md` — current completion baseline, limitations, and next authorized boundary.
