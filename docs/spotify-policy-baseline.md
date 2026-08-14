# Spotify policy and Web API baseline

Verified against official Spotify for Developers pages on **2026-08-14**. This is an engineering risk
record, not legal advice. Reverify before Phase 2 and before every material production change.

## Recorded policy risk and user decision

Spotify's [Developer Policy](https://developer.spotify.com/policy) (effective 2025-05-15) and
[Developer Terms](https://developer.spotify.com/terms) (version 10, effective 2025-05-15) prohibit using
the Spotify Platform or Spotify Content to train an AI/ML model or otherwise ingest Spotify Content into
an AI/ML model. The definitions and restrictions are broad enough that this project's intended use of
Spotify-originated title/artist identity in a Codex-assisted workflow carries a known policy risk.

For this personal, non-commercial project, the user has explicitly directed the project to accept that
risk and proceed with this architecture: Spotify may supply title and artist identity; those identities may
drive independent research; Codex may consume the identity, independently researched information, and
playlist specifications to make proposals. This record does **not** claim that architecture is compliant,
and it does not waive Spotify's right to restrict or revoke access.

The implementation must remain replaceable at both boundaries: another platform can replace Spotify, and
another rules/model implementation can replace Codex. Spotify-specific fields must not leak into the
classifier's conceptual model.

## No concealment or evasion

The project will not conceal automation, imitate human behavior, rotate accounts or proxies, manipulate
fingerprints, bypass CAPTCHAs/security controls, defeat monitoring, or evade enforcement. It will use normal
API engineering: cache state, paginate, batch within documented limits, avoid redundant scans, use playlist
snapshots where helpful, back off, honor `Retry-After`, reconcile ambiguous failures, and stop on quota
exhaustion. Natural pauses while local research/review occurs require no Spotify traffic.

## Current platform constraints relevant to V1

- Spotify [Development Mode](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)
  currently requires the app owner to have Premium, permits at most five allowlisted users, and applies quota
  to the developer account. Spotify's 2026-07-23
  [quota update](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates) limits developers to
  25 Client IDs/apps, shared across Development Mode and Extended Quota Mode.
- The 2026-02 [Web API changelog](https://developer.spotify.com/documentation/web-api/references/changes/february-2026)
  replaced playlist `/tracks` operations with `/items`; creation is now `POST /me/playlists`; search limits
  were reduced; popularity was removed. A 2026-03 change restored track `external_ids`. Code must follow the
  current reference rather than old tutorials.
- [Get Playlist Items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items)
  is currently limited to playlists owned by the current user or playlists where that user is a collaborator;
  other playlists return `403`. Third-party reference playlists therefore need explicit user import (for
  example synthetic-schema CSV/JSON/text) or exclusion—never scraping/browser automation.
- [Get a User's Saved Tracks](https://developer.spotify.com/documentation/web-api/reference/get-users-saved-tracks)
  models Liked Songs as the user's saved-track library, not a playlist. A later read-only phase will use
  `user-library-read` and retain its own processed state; it will not remove saved tracks.
- [Create Playlist](https://developer.spotify.com/documentation/web-api/reference/create-playlist) uses
  `POST /me/playlists`. [Add Items](https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist)
  uses `POST /playlists/{playlist_id}/items`, accepts at most 100 URIs per request, and returns a snapshot ID.
  The API also exposes removal, replacement/reorder, and detail changes, but V1 policy prohibits them.
- Spotify [rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits) use a rolling
  30-second window. A `429` response may include `Retry-After`; Development Mode quota exhaustion can be
  identified separately and should stop work rather than trigger aggressive retries.
- Playlist snapshot IDs are version identifiers. Later ingestion may skip known-unchanged sources and later
  reconciliation may use snapshots, but a snapshot never authorizes a write.

## Authentication baseline for a later phase

No authentication exists in Phase 0. For a local desktop/CLI client that cannot safely retain a client
secret, Spotify's [Authorization overview](https://developer.spotify.com/documentation/web-api/concepts/authorization)
and [PKCE guide](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) support
Authorization Code with PKCE, refresh tokens, exact redirect-URI allowlisting, and a random `state` value.
A loopback callback is the likely V1 approach, subject to reverification in Phase 2.

Anticipated least-privilege scopes are `user-library-read`, `playlist-read-private`, and
`playlist-read-collaborative` for configured inputs, plus only the modify scope(s) needed for the chosen
managed-playlist visibility when writes are later approved. The current
[scope reference](https://developer.spotify.com/documentation/web-api/concepts/scopes) associates private
and public playlist mutations with `playlist-modify-private` and `playlist-modify-public`. Do not request
email, listening history, playback, recommendations, image, library-write, or unrelated profile scopes.

Access and refresh tokens must use OS-appropriate protected credential storage, never `.env`, repository
files, logs, prompts, or SQLite. Logout/reset must delete local credentials and support user revocation.

## Reverification gates

Recheck all cited pages before Phase 2, Phase 7, and any release. Stop if Development Mode eligibility,
playlist accessibility, OAuth behavior, endpoint paths, scopes, policy wording, or write semantics differ.
