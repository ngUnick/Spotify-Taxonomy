# Spotify setup, credentials, and read checkpoints

Phases 2 and 3 are complete. These instructions do not authorize a new login or API call; every future live
checkpoint still requires explicit approval.

## Dashboard configuration

1. Create/select the personal Development Mode application in Spotify for Developers.
2. Register the redirect URI exactly as `http://127.0.0.1:8888/callback`. Do not use `localhost`.
   If `spotify.callback_port` or `spotify auth login --callback-port` selects another port, register the
   exact equivalent URI containing that same port before login.
3. Keep the Client ID in `SPOTIFY_CLIENT_ID` or pass `--client-id`. Do not create or configure a client secret.
4. Do not put tokens, the Client ID, or authorization responses in repository configuration.

The application binds exactly `127.0.0.1:<callback_port>` and never falls back to another port. The default
port is `8888`; valid configured ports are `1024` through `65535`. If the port is unavailable, login fails
closed so the local listener cannot diverge from the Redirect URI registered in Spotify. It requests exactly:

```text
user-read-private
playlist-read-private
playlist-read-collaborative
user-library-read
```

## Commands

```powershell
spotify-taxonomy spotify auth status
spotify-taxonomy spotify auth login
spotify-taxonomy spotify auth login --callback-port 9443
spotify-taxonomy spotify auth login --no-browser
spotify-taxonomy spotify auth login --managed-provisioning
spotify-taxonomy spotify auth logout
spotify-taxonomy spotify check
```

`--no-browser` prints the complete, short-lived authorization URL directly to the local terminal so the user
can open it. That URL is never logged or persisted. The PKCE verifier is never printed. Ordinary output masks
Client ID and account identifiers.

`--managed-provisioning` is the explicit Phase 10 reauthorization path for the sole additional scope,
`playlist-modify-private`. It does not request `playlist-modify-public`. The Spotify scope does not broaden
the application boundary: Phase 10 exposes only one creation that sends best-effort `public=false`, plus durable local
registration, never item, removal, rename, delete, or generic mutation operations.

The callback-port precedence is command-level `--callback-port`, then `spotify.callback_port` in application
configuration, then the default `8888`. Authorization and token exchange use the identical exact redirect URI.

`spotify check` performs only token refresh when needed and `GET /me`; do not run it before the separately
approved real-authentication checkpoint. Playlist and saved-track reads are library interfaces for Phase 3,
not Phase 2 CLI ingestion commands.

## Protected storage and revocation

Only the refresh token, granted scopes, and authorization timestamp are stored. On Windows they use the
concrete built-in/core Windows Credential Locker implementation supplied by the approved `keyring` version.
The application fails closed instead of using null, fail, file, configured, or third-party backends. Access
tokens and all PKCE flow values are memory-only.

`spotify auth logout` deletes the local protected credential. To revoke Spotify-side authorization too,
remove the application from the connected-apps area of the Spotify account settings.

## Phase 3 command boundary

Phase 3 introduced stable `account_id` binding, ephemeral playlist discovery, explicit SOURCE/INBOX
registration, publication-safe read-only scans, and local account-scoped catalogue review. The completed
live checkpoints covered one playlist SOURCE, the singleton Saved Tracks/Liked Songs INBOX, and one
explicitly selected playlist-backed Shazam INBOX. A playlist name never conferred registration or INBOX
authority; explicit selection was resolved to a stable ID before registration.

These commands may contact `/me`, `/me/playlists`, `/playlists/{id}`, `/playlists/{id}/items`, or
`/me/tracks` and must not be run again without a separately approved read boundary. They never call a
Spotify mutation endpoint. The application requests no modify scope and has no Spotify writer.

Phase 3 Checkpoint B observed canonical nested `item` across the complete 48-entry controlled playlist and
no legacy `track` variant. The compatibility fallback was removed; canonical `item` is authoritative, and
the deprecated `/playlists/{id}/tracks` endpoint remains prohibited.
