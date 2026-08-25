# Spotify Playlist Taxonomy

Phases 0 through 7 are complete. Phase 6 adds an offline, deterministic and auditable path from persisted
research evidence to versioned derived characteristics and explainable semantic proposals; proposals are not
user-approved assignments. Unknown or insufficient evidence abstains rather than inferring a result, and
probable, contextual, stale, or contradictory evidence is never silently promoted.
Phase 7 adds auditable user review/overrides and effective reviewed state: explicit user decisions outrank
generated proposals without erasing them, and clearing a decision reveals its generated state again.
Calibration is observational only; it never tunes rules or configuration. Phase 8 has not started.
live provider access remains an explicit operator action. Phase 4 provides an explicitly human-reviewed, source-controlled
58-characteristic vocabulary, one reviewed specification for each of the 36 stable semantic playlists,
centralized overlap guidance, 51 stable future output targets, an approved Greek/Foreign Genre routing
policy, and strict offline cross-document validation.

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

Phase 5 can gather compact provenance-aware evidence from MusicBrainz and Wikidata only through an explicit
bounded `research run ... --network` command. Offline evidence/status inspection never contacts a provider.
It does **not** assign tracks, route Genre variants, create desired state, or modify Spotify. Runtime
Greek/Foreign routing remains downstream of semantic classification.

For research-ready, safely mapped canonicals with a locally persisted ISRC, exact MusicBrainz Recording
grounding is a separate bounded operator workflow: `research grounding acquire preview --limit N` is local
and read-only, while `research grounding acquire fetch --network --limit N` explicitly persists only the
existing identity-scoped Recording-evidence states. The ISRC is used solely to resolve that research subject;
it is never classification evidence or canonical-match authority. Acquisition neither runs Phase 5 research
nor derives, classifies, or changes matching state.

The 15 Genre meanings remain unsplit. Each independently approved Genre membership will later route to a
Greek or Foreign output target using exact-recording language and cultural-context evidence. Foreign means
all non-Greek languages and contexts, not only English; unresolved routing remains reviewable.

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
spotify-taxonomy catalogue match list --state ambiguous --limit 25
spotify-taxonomy catalogue match show CANDIDATE_ID
spotify-taxonomy catalogue match evidence-status
spotify-taxonomy catalogue match evidence-list --category same-isrc-duration-gt-5s --limit 25
spotify-taxonomy catalogue match deterministic preview --eligible-only --limit 25
spotify-taxonomy catalogue match deterministic apply --limit 5
spotify-taxonomy catalogue match deterministic history --limit 25
spotify-taxonomy catalogue match evidence-backfill
spotify-taxonomy catalogue match confirm-existing CANDIDATE_ID CANONICAL_ID
spotify-taxonomy catalogue match confirm-new CANDIDATE_ID
spotify-taxonomy catalogue match reject CANDIDATE_ID
spotify-taxonomy catalogue link list
spotify-taxonomy catalogue link unlink IDENTITY_ID
spotify-taxonomy research run CANONICAL_ID... --network
spotify-taxonomy research grounding acquire preview --limit 10
spotify-taxonomy research grounding acquire fetch --network --limit 10
spotify-taxonomy research show CANONICAL_ID
spotify-taxonomy research status [CANONICAL_ID]
spotify-taxonomy review decide CANONICAL_ID SEMANTIC_ID include
spotify-taxonomy review batch import REVIEW_BATCH.json
```

For a bounded local explicit-review import, use JSON such as
`{"schema":"phase7-review-batch.v1","decisions":[{"canonical_id":"canonical.synthetic.example","semantic_id":"vibe.chill","decision":"include","note":"optional human note"}]}`.
The import accepts 1–25 decisions and writes ordinary Phase 7 explicit review events transactionally; it does
not contact Spotify or any provider.

For explicit Genre output routing without classifying Genre, use `spotify-taxonomy routing batch import ROUTING_BATCH.json` with
`{"schema":"genre-routing-batch.v1","decisions":[{"canonical_id":"canonical.synthetic.example","route":"foreign","note":"optional human note"}]}`.
It accepts 1–25 canonical-specific `greek|foreign` decisions atomically. Inspect the current local state with
`spotify-taxonomy routing show CANONICAL_ID`; routing affects a desired target only after an effective Genre include.

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
- `docs/characteristic-vocabulary.md` — controlled characteristic families and evidence semantics.
- `docs/taxonomy-specifications.md` — reviewed specifications, overlaps, and semantic/output boundaries.
- `docs/spotify-policy-baseline.md` — dated Spotify API/policy risk record.
- `docs/spotify-setup.md` — least-privilege dashboard, PKCE, credential, and revocation guidance.
- `docs/project-handoff.md` — current completion baseline, limitations, and next authorized boundary.
