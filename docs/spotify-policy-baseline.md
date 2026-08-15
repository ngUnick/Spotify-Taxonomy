# Spotify policy and Web API baseline

Verified against official Spotify for Developers pages on **2026-08-14** for the Phase 2 offline
implementation. This is an engineering risk record, not legal advice. Reverify before the first controlled
live checkpoint and every material production change.

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
- [Get Current User's Profile](https://developer.spotify.com/documentation/web-api/reference/get-current-users-profile)
  currently associates `GET /me` with `user-read-private`. Phase 2 retains only immutable `account_id` and
  the current Spotify user ID; it does not request or retain email or other profile fields.
- Current playlist schemas permit `public: null`, and playlist `items`/count metadata can be absent for
  followed playlists whose contents are unavailable. The adapter preserves these states as `None` rather
  than inventing false/zero values.

## Phase 2 authentication baseline

Spotify's [PKCE guide](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) and
[redirect URI rules](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri) support a
desktop client without a client secret, an S256 challenge, random state, and an IPv4 loopback callback.
Register `http://127.0.0.1/callback`; Phase 2 chooses its port transiently after binding to loopback.

The exact Phase 2 scopes are `user-read-private`, `playlist-read-private`,
`playlist-read-collaborative`, and `user-library-read`. No email or modify scope is requested. Access tokens
remain in memory. Refresh tokens use the concrete built-in/core Windows Credential Locker backend supplied
by the approved `keyring` package, never configured third-party or file backends.

The Client ID may remain in CLI/environment process configuration for login and refresh but is not written
to YAML, SQLite, or logs. State, verifier, challenge, redirect URI, authorization URL, and code exist only
during login. `--no-browser` may deliberately print the complete authorization URL to the local terminal;
the verifier is never displayed. Logout deletes the local credential; Spotify account settings provide
server-side revocation.

Spotify documents finite refresh-token lifetime and `invalid_grant` for expired/revoked authorization.
Phase 2 fails closed, deletes an unusable stored token, and requires explicit reauthorization.

## Playlist schema compatibility record

The current `/playlists/{id}/items` reference and 2026 migration material identify nested `item` as the
canonical field, while parts of the official reference remain inconsistent. Phase 2 never calls the removed
`/playlists/{id}/tracks` endpoint. It includes one isolated nested-`track` parser used only when `item` is
absent, prefers `item` whenever present, and records metadata-free variant counts in memory. Reassess and
potentially remove this fallback after Phase 3's first controlled playlist read.

## Reverification gates

Recheck all cited pages before the first live checkpoint, Phase 3, each write phase, and any release. Stop if Development Mode eligibility,
playlist accessibility, OAuth behavior, endpoint paths, scopes, policy wording, or write semantics differ.
