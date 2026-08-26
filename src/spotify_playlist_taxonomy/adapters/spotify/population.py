"""Narrow Phase 11 Spotify item-add adapter; no generic playlist mutation."""
from __future__ import annotations

import re
from typing import Protocol

import httpx

from .errors import SpotifyAdapterError, SpotifyErrorKind

POPULATION_SCOPES = ("playlist-modify-private", "playlist-modify-public")
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]+$")


class TokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


class SpotifyManagedTrackAdder:
    """Adds exactly one track to an exact managed-playlist identity."""
    def __init__(self, *, token_provider: TokenProvider, http_client: httpx.Client) -> None:
        self._tokens, self._http = token_provider, http_client

    def add_one_managed_track(self, playlist_id: str, provider_track_id: str) -> None:
        if not _SPOTIFY_ID.fullmatch(playlist_id) or not _SPOTIFY_ID.fullmatch(provider_track_id):
            raise SpotifyAdapterError(SpotifyErrorKind.REQUEST_NOT_SENT, "request_not_sent: managed addition identity is invalid")
        token = self._tokens.access_token()
        try:
            response = self._http.post(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
                headers={"Authorization": f"Bearer {token}"},
                json={"uris": [f"spotify:track:{provider_track_id}"]},
            )
        except httpx.HTTPError:
            raise SpotifyAdapterError(SpotifyErrorKind.REMOTE_OUTCOME_UNKNOWN, "remote_outcome_unknown: managed addition outcome is unknown") from None
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_seconds = int(retry_after) if retry_after is not None else None
            except ValueError:
                retry_after_seconds = None
            if retry_after_seconds is not None and retry_after_seconds < 0:
                retry_after_seconds = None
            raise SpotifyAdapterError(
                SpotifyErrorKind.QUOTA_EXHAUSTED,
                "Spotify API quota is exhausted; managed addition was not dispatched",
                reason="QUOTA_EXCEEDED", retry_after_seconds=retry_after_seconds,
            )
        if 400 <= response.status_code < 500:
            raise SpotifyAdapterError(SpotifyErrorKind.PROVIDER_REJECTED, "provider_rejected: managed addition was rejected")
        if response.status_code != 201:
            raise SpotifyAdapterError(SpotifyErrorKind.REMOTE_OUTCOME_UNKNOWN, "remote_outcome_unknown: managed addition outcome is unknown")
        try:
            payload = response.json()
        except ValueError:
            raise SpotifyAdapterError(SpotifyErrorKind.INVALID_SUCCESS_RESPONSE, "invalid_success_response: provider acknowledged addition without usable confirmation") from None
        snapshot = payload.get("snapshot_id") if isinstance(payload, dict) else None
        if not isinstance(snapshot, str) or not snapshot:
            raise SpotifyAdapterError(SpotifyErrorKind.INVALID_SUCCESS_RESPONSE, "invalid_success_response: provider acknowledged addition without usable confirmation")
