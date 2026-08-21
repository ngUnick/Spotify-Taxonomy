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
    REQUEST_NOT_SENT = "request_not_sent"
    PROVIDER_REJECTED = "provider_rejected"
    REMOTE_OUTCOME_UNKNOWN = "remote_outcome_unknown"
    INVALID_SUCCESS_RESPONSE = "invalid_success_response"
    PROVIDER_POLICY_MISMATCH = "provider_policy_mismatch"


class SpotifyAdapterError(PlatformReaderError):
    """A user-safe error that never includes response bodies or secrets."""

    def __init__(
        self,
        kind: SpotifyErrorKind,
        message: str,
        *,
        reason: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, int)
            or retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds must be a non-negative integer or None")
        self.kind = kind
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        access_state = {
            SpotifyErrorKind.FORBIDDEN: PlatformAccessState.FORBIDDEN,
            SpotifyErrorKind.NOT_FOUND: PlatformAccessState.NOT_FOUND,
        }.get(kind)
        super().__init__(message, access_state=access_state)
