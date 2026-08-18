"""Narrow Spotify adapter for Phase 10 private managed-playlist creation only."""
from __future__ import annotations

import re
from typing import Protocol

import httpx

from .errors import SpotifyAdapterError, SpotifyErrorKind

PROVISIONING_SCOPE = "playlist-modify-private"
MANAGED_DESCRIPTION = "Managed by Spotify Taxonomy."
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9]+$")


class TokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


class SpotifyManagedPlaylistPolicyMismatch(SpotifyAdapterError):
    """A confirmed playlist identity whose returned policy violates Phase 10."""

    def __init__(self, playlist_id: str) -> None:
        self.playlist_id = playlist_id
        super().__init__(
            SpotifyErrorKind.PROVIDER_POLICY_MISMATCH,
            "provider_policy_mismatch: provider-confirmed playlist violates managed privacy policy",
        )


class SpotifyManagedPlaylistProvisioner:
    """Creates one private non-collaborative playlist; it has no item mutation methods."""
    def __init__(self, *, token_provider: TokenProvider, http_client: httpx.Client) -> None:
        self._tokens, self._http = token_provider, http_client

    def create_private_managed_playlist(self, account_id: str, name: str) -> str:
        if not account_id or not name.strip():
            raise SpotifyAdapterError(
                SpotifyErrorKind.REQUEST_NOT_SENT,
                "request_not_sent: managed playlist request is invalid",
            )
        try:
            token = self._tokens.access_token()
        except SpotifyAdapterError:
            # Token acquisition completes before a POST can be dispatched.
            raise
        try:
            response = self._http.post(
                "https://api.spotify.com/v1/me/playlists",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": name, "description": MANAGED_DESCRIPTION, "public": False, "collaborative": False},
            )
        except httpx.HTTPError:
            # httpx cannot prove whether bytes reached the provider, so fail closed.
            raise SpotifyAdapterError(
                SpotifyErrorKind.REMOTE_OUTCOME_UNKNOWN,
                "remote_outcome_unknown: managed playlist creation outcome is unknown",
            ) from None
        if 400 <= response.status_code < 500:
            raise SpotifyAdapterError(
                SpotifyErrorKind.PROVIDER_REJECTED,
                "provider_rejected: managed playlist creation was rejected",
            )
        if response.status_code != 201:
            raise SpotifyAdapterError(
                SpotifyErrorKind.REMOTE_OUTCOME_UNKNOWN,
                "remote_outcome_unknown: managed playlist creation outcome is unknown",
            )
        try:
            payload = response.json()
        except ValueError:
            raise SpotifyAdapterError(
                SpotifyErrorKind.INVALID_SUCCESS_RESPONSE,
                "invalid_success_response: provider confirmed creation but returned no usable identity",
            ) from None
        if not isinstance(payload, dict):
            raise SpotifyAdapterError(
                SpotifyErrorKind.INVALID_SUCCESS_RESPONSE,
                "invalid_success_response: provider confirmed creation but returned no usable identity",
            )
        playlist_id = payload.get("id")
        if not isinstance(playlist_id, str) or not _PLAYLIST_ID.fullmatch(playlist_id):
            raise SpotifyAdapterError(
                SpotifyErrorKind.INVALID_SUCCESS_RESPONSE,
                "invalid_success_response: provider confirmed creation but returned no usable identity",
            )
        if payload.get("collaborative") is not False:
            raise SpotifyManagedPlaylistPolicyMismatch(playlist_id)
        return playlist_id
