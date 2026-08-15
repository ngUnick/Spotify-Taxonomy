"""Sanitized Spotify adapter failures."""

from __future__ import annotations

from enum import StrEnum

from spotify_playlist_taxonomy.ports.music_platform import (
    PlatformAccessState,
    PlatformReaderError,
)


class SpotifyErrorKind(StrEnum):
    CONFIGURATION = "configuration"
    CREDENTIAL_BACKEND = "credential_backend"
    NOT_AUTHORIZED = "not_authorized"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_TIMEOUT = "authorization_timeout"
    AUTHORIZATION_FAILED = "authorization_failed"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    INVALID_CURSOR = "invalid_cursor"


class SpotifyAdapterError(PlatformReaderError):
    """A user-safe error that never includes response bodies or secrets."""

    def __init__(self, kind: SpotifyErrorKind, message: str) -> None:
        self.kind = kind
        access_state = {
            SpotifyErrorKind.FORBIDDEN: PlatformAccessState.FORBIDDEN,
            SpotifyErrorKind.NOT_FOUND: PlatformAccessState.NOT_FOUND,
        }.get(kind)
        super().__init__(message, access_state=access_state)
