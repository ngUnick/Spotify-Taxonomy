# Project handoff

## Repository state

- Repository: `C:\Users\USER\Desktop\Projects\Spotify-Taxonomy`
- Branch: `dev`
- Completed phases: 0, 1, 2, 3, 4, and 5
- Current boundary: Phase 5 is complete; Phase 6 is next and has not started

## Phase 3 completion baseline

The final read-only audit confirmed one active stable Spotify `account_id` binding and three active ingestion
sources. Each source has exactly one authoritative publication, every authoritative scan is completed and
consistency-checked, and no noncompleted scan contributes to matching:

| Source type | Role | Published occurrences |
| --- | --- | ---: |
| Registered playlist | SOURCE | 48 |
| Singleton Saved Tracks/Liked Songs | INBOX | 506 |
| Explicitly selected playlist-backed Shazam source | INBOX | 252 |
| **Total** |  | **806** |

The published catalogue contains 744 distinct provider identities, 740 portable canonical tracks, and 744
active account-scoped identity links. There are no staged-only identities, invalid authoritative
publications, review-required identities, or actionable candidates. Four ambiguous duplicate cases were
explicitly confirmed by the user as links to existing canonical tracks; the four selected candidates are
recorded with `user_confirmation`, their four create-new alternatives are superseded, and no new canonical
track was created by those decisions. Link history contains 744 events: 740 system bootstraps and four user
confirmations.

Provider identities and canonical tracks are global/portable. Source state, identity adoption, active links,
matching candidates, interventions, and decision history are scoped to the stable account binding. Playlist
discovery is ephemeral; a stable ID must be explicitly selected and registered before it becomes a source.
The Saved Tracks/Liked Songs source is a singleton library INBOX. A playlist name never grants Shazam INBOX
authority.

Playlist ingestion accepts only the current `/playlists/{id}/items` response with canonical nested `item`.
The legacy nested-`track` fallback was removed after live validation. SOURCE and INBOX inputs remain
immutable/read-only. Phase 3 requested no Spotify modify scope and introduced no Spotify writer.

SQLite integrity is `ok` with zero foreign-key violations. Real runtime data and protected credentials live
outside Git under the approved per-user runtime and credential backends. No real title, artist, playlist ID,
account ID, raw response, token, database, log, backup, export, or batch is source-controlled.

## Known limitations and deferred work

- MusicBrainz and Wikidata have completed their authorized local provider checkpoint. Research remains
  evidence-only; no classification has occurred.
- Saved Tracks has no snapshot identifier, so its complete scan uses beginning/end total and initial-page
  signature checks rather than playlist-strength snapshot consistency.
- The current Spotify platform may restrict playlist item access even when metadata discovery succeeds.
- Desired state, dry-run synchronization, managed provisioning, guarded addition-only execution, and
  operation journaling remain later-phase designs only.
- V1 remains manually invoked and user supervised; there is no unattended scheduler or destructive
  reconciliation.

## Remaining roadmap

5. Add derived characteristics and deterministic rules.
6. Add review, overrides, and calibration.
7. Add Codex supervised file-exchange classification.
9. Add desired state / dry-run synchronization.
10. Run a managed-playlist provisioning pilot.
11. Add guarded addition-only population.
12. Add periodic inbox processing / V1 hardening.

## Phase 4 completion

Application configuration schema v3 references a source-controlled, explicitly reviewed
`characteristics.v1` vocabulary and specification manifest. The manifest covers exactly the unchanged 36
stable logical playlist IDs and maps them to 15 Genre, 10 Vibe, and 11 Activity specification files plus
centralized overlap guidance. The 15
Genre meanings remain semantic definitions; a separate registry expands them to Greek and Foreign targets,
while Vibe and Activity remain unsplit, for 51 future concrete targets. The vocabulary contains 58 reusable
characteristics in 13 families, including version-sensitive language and cultural-context routing inputs.
Strict offline loading validates paths, versions, statuses, references, typed operators/values, required
overlap groups, target coverage, the closed routing policy, and a deterministic full-bundle digest.

All 36 specifications, the manifest, and the 58-characteristic vocabulary are `reviewed`. Greek/Foreign
routing remains downstream from semantic Genre classification; balanced, missing, and genuinely ambiguous
routing remains unresolved for later human review rather than defaulting to Foreign. Phase 4 added no
migration or runtime synchronization and performed no real classification, research, or Spotify access.
The source bundle contains no track assignments or real catalogue/account metadata.

## Phase 5 implementation boundary

Migration 5 and the local coordinator persist compact provider assertions, normalized evidence provenance,
cache/freshness state, and research attempts for explicit canonical IDs. MusicBrainz is limited to structured
recording/version/work/release/credit relationships without tags or ratings. Wikidata is limited to supported
structured statements and preserves contextual work/project scope. `research show` and `research status` are
offline; `research run` requires explicit `--network` and a maximum of 25 IDs. No raw provider response,
characteristic, playlist proposal, Genre variant result, or Spotify capability is produced.

## Exact next action

Phase 6 — derived characteristics and deterministic rules. Do not begin without separate authorization.

## Continuing safety boundary

Any new Spotify read requires a separately approved endpoint boundary. Spotify writes and new OAuth scopes
remain unauthorized. Research remains evidence-only and classification must remain
independent of the Spotify mutation path. Explicit user decisions outrank generated proposals. SOURCE and
INBOX sources must never be modified, and playlist names must never confer authority. Future writes, if
separately implemented and approved in later phases, may target only stable-ID MANAGED playlists through
the dry-run, approval, addition-only, verification path. Runtime databases, logs, backups, batches, exports,
research caches, credentials, raw responses, and real music/account metadata must remain outside the
repository.
