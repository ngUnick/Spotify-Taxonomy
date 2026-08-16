# Project handoff

## Repository state

- Repository: `C:\Users\USER\Desktop\Projects\Spotify-Taxonomy`
- Branch: `dev`
- Current HEAD: `35332a50694e879ebbce1dac33121c821043d109`
- Remote baseline: `dev` synchronized with `origin/dev` before these documentation-only changes
- Completed phases: 0, 1, 2, and 3
- Current boundary: Phase 4 is next and has not started

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

- Phase 4 taxonomy specifications and characteristic vocabulary are not finalized.
- No external research provider, research cache, sufficiency workflow, or live classification exists.
- Saved Tracks has no snapshot identifier, so its complete scan uses beginning/end total and initial-page
  signature checks rather than playlist-strength snapshot consistency.
- The current Spotify platform may restrict playlist item access even when metadata discovery succeeds.
- Desired state, dry-run synchronization, managed provisioning, guarded addition-only execution, and
  operation journaling remain later-phase designs only.
- V1 remains manually invoked and user supervised; there is no unattended scheduler or destructive
  reconciliation.

## Remaining roadmap

4. Finalize V1 taxonomy specifications and characteristic vocabulary.
5. Add independent research providers, caching, provenance, and sufficiency evaluation.
6. Add derived characteristics and deterministic rules.
7. Add review, overrides, and calibration.
8. Add Codex supervised file-exchange classification.
9. Add desired state / dry-run synchronization.
10. Run a managed-playlist provisioning pilot.
11. Add guarded addition-only population.
12. Add periodic inbox processing / V1 hardening.

## Exact next action

Prepare and finalize the Phase 4 plan for **Phase 4 — taxonomy specifications and characteristic
vocabulary**. Do not implement Phase 4, access a research provider, classify real catalogue data, or expand
the taxonomy until the user explicitly authorizes that phase.

## Continuing safety boundary

Any new Spotify read requires a separately approved endpoint boundary. Spotify writes and new OAuth scopes
remain unauthorized. Research and classification have not started, and classification must remain
independent of the Spotify mutation path. Explicit user decisions outrank generated proposals. SOURCE and
INBOX sources must never be modified, and playlist names must never confer authority. Future writes, if
separately implemented and approved in later phases, may target only stable-ID MANAGED playlists through
the dry-run, approval, addition-only, verification path. Runtime databases, logs, backups, batches, exports,
research caches, credentials, raw responses, and real music/account metadata must remain outside the
repository.
