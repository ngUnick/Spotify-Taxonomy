"""Spotify Authorization Code with PKCE and in-memory access-token handling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from urllib.parse import urlencode
import webbrowser

import httpx

from .callback import (
    AuthorizationCallback,
    DEFAULT_CALLBACK_PORT,
    LoopbackCallbackServer,
    validate_callback_port,
)
from .credentials import CredentialEnvelope, CredentialStore
from .errors import SpotifyAdapterError, SpotifyErrorKind

ACCOUNTS_ORIGIN = "https://accounts.spotify.com"
AUTHORIZE_URL = f"{ACCOUNTS_ORIGIN}/authorize"
TOKEN_URL = f"{ACCOUNTS_ORIGIN}/api/token"
REQUIRED_SCOPES = (
    "user-read-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)
REFRESH_EARLY_SECONDS = 60


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str
    expires_at: datetime
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.value or self.expires_at.tzinfo is None:
            raise ValueError("invalid access token")


@dataclass(frozen=True, slots=True)
class AuthorizationStatus:
    authorized: bool
    scopes: tuple[str, ...] = ()
    authorized_at: datetime | None = None


def resolve_client_id(
    cli_client_id: str | None,
    environ: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if environ is None else environ
    value = cli_client_id if cli_client_id is not None else environment.get("SPOTIFY_CLIENT_ID")
    if value is None or not value.strip():
        raise SpotifyAdapterError(
            SpotifyErrorKind.CONFIGURATION,
            "Spotify Client ID is required via --client-id or SPOTIFY_CLIENT_ID",
        )
    return value.strip()


def mask_identifier(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _default_callback_factory(port: int) -> AuthorizationCallback:
    return LoopbackCallbackServer(port)


class SpotifyAuthManager:
    """Own one process's transient authorization and access-token state."""

    def __init__(
        self,
        *,
        client_id: str,
        credential_store: CredentialStore,
        http_client: httpx.Client,
        callback_timeout_seconds: float = 300,
        callback_port: int = DEFAULT_CALLBACK_PORT,
        callback_factory: Callable[[int], AuthorizationCallback] = _default_callback_factory,
        browser_open: Callable[[str], bool] = webbrowser.open,
        authorization_url_sink: Callable[[str], None] = lambda value: None,
        warning_sink: Callable[[str], None] = lambda value: None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not client_id.strip():
            raise SpotifyAdapterError(SpotifyErrorKind.CONFIGURATION, "Spotify Client ID is required")
        validate_callback_port(callback_port)
        self._client_id = client_id
        self._credentials = credential_store
        self._http = http_client
        self._callback_timeout = callback_timeout_seconds
        self._callback_port = callback_port
        self._callback_factory = callback_factory
        self._browser_open = browser_open
        self._authorization_url_sink = authorization_url_sink
        self._warning_sink = warning_sink
        self._now = now
        self._access_token: AccessToken | None = None

    def status(self) -> AuthorizationStatus:
        stored = self._credentials.load(self._client_id)
        if stored is None:
            return AuthorizationStatus(authorized=False)
        return AuthorizationStatus(True, stored.scopes, stored.authorized_at)

    def login(self, *, no_browser: bool = False) -> AccessToken:
        callback = self._callback_factory(self._callback_port)
        state: str | None = None
        verifier: str | None = None
        challenge: str | None = None
        authorization_url: str | None = None
        redirect_uri: str | None = None
        result = None
        try:
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = _pkce_challenge(verifier)
            redirect_uri = callback.redirect_uri
            authorization_url = _authorization_url(self._client_id, redirect_uri, state, challenge)
            if no_browser:
                self._authorization_url_sink(authorization_url)
            elif not self._browser_open(authorization_url):
                raise SpotifyAdapterError(
                    SpotifyErrorKind.AUTHORIZATION_FAILED,
                    "Unable to open the Spotify authorization page",
                )

            result = callback.wait(self._callback_timeout)
            if result.error is not None:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.AUTHORIZATION_DENIED,
                    "Spotify authorization was denied",
                )
            if result.state is None or not hmac.compare_digest(result.state, state):
                raise SpotifyAdapterError(
                    SpotifyErrorKind.AUTHORIZATION_FAILED,
                    "Spotify authorization state validation failed",
                )
            if not result.code:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.AUTHORIZATION_FAILED,
                    "Spotify authorization did not return a code",
                )

            payload = self._token_request(
                {
                    "client_id": self._client_id,
                    "grant_type": "authorization_code",
                    "code": result.code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                }
            )
            access, refresh_token = self._parse_token_payload(
                payload, default_scopes=REQUIRED_SCOPES, require_refresh_token=True
            )
            missing = set(REQUIRED_SCOPES) - set(access.scopes)
            if missing:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.AUTHORIZATION_FAILED,
                    "Spotify did not grant all required read scopes",
                )
            self._warn_extra_scopes(access.scopes)
            self._credentials.save(
                self._client_id,
                CredentialEnvelope(
                    refresh_token=refresh_token,
                    scopes=access.scopes,
                    authorized_at=self._now(),
                ),
            )
            self._access_token = access
            return access
        finally:
            callback.close()
            # Make the intended lifetime explicit; values are never persisted or logged.
            result = None
            state = verifier = challenge = authorization_url = redirect_uri = None

    def logout(self) -> bool:
        self._access_token = None
        return self._credentials.delete(self._client_id)

    def access_token(self, *, force_refresh: bool = False) -> str:
        threshold = self._now() + timedelta(seconds=REFRESH_EARLY_SECONDS)
        if (
            not force_refresh
            and self._access_token is not None
            and self._access_token.expires_at > threshold
        ):
            return self._access_token.value
        return self._refresh().value

    def _refresh(self) -> AccessToken:
        stored = self._credentials.load(self._client_id)
        if stored is None:
            raise SpotifyAdapterError(
                SpotifyErrorKind.NOT_AUTHORIZED,
                "Spotify authorization is required",
            )
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": stored.refresh_token,
                },
            )
        except httpx.HTTPError:
            raise SpotifyAdapterError(
                SpotifyErrorKind.NETWORK,
                "Spotify token refresh failed due to a network error",
            ) from None
        payload = _safe_json_mapping(response)
        if response.status_code >= 400:
            if payload.get("error") == "invalid_grant":
                self._credentials.delete(self._client_id)
                self._access_token = None
                raise SpotifyAdapterError(
                    SpotifyErrorKind.NOT_AUTHORIZED,
                    "Spotify authorization expired or was revoked; login is required",
                )
            raise SpotifyAdapterError(
                SpotifyErrorKind.AUTHORIZATION_FAILED,
                "Spotify token refresh failed",
            )
        access, replacement = self._parse_token_payload(
            payload, default_scopes=stored.scopes, require_refresh_token=False
        )
        missing = set(REQUIRED_SCOPES) - set(access.scopes)
        if missing:
            raise SpotifyAdapterError(
                SpotifyErrorKind.AUTHORIZATION_FAILED,
                "Stored Spotify authorization lacks required read scopes",
            )
        self._warn_extra_scopes(access.scopes)
        self._credentials.save(
            self._client_id,
            CredentialEnvelope(
                refresh_token=replacement or stored.refresh_token,
                scopes=access.scopes,
                authorized_at=stored.authorized_at,
            ),
        )
        self._access_token = access
        return access

    def _token_request(self, data: dict[str, str]) -> dict[str, object]:
        try:
            response = self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError:
            raise SpotifyAdapterError(
                SpotifyErrorKind.NETWORK,
                "Spotify authorization exchange failed due to a network error",
            ) from None
        payload = _safe_json_mapping(response)
        if response.status_code >= 400:
            raise SpotifyAdapterError(
                SpotifyErrorKind.AUTHORIZATION_FAILED,
                "Spotify authorization exchange failed",
            )
        return payload

    def _parse_token_payload(
        self,
        payload: dict[str, object],
        *,
        default_scopes: tuple[str, ...],
        require_refresh_token: bool,
    ) -> tuple[AccessToken, str]:
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        refresh_token = payload.get("refresh_token", "")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or expires_in <= 0
            or not isinstance(refresh_token, str)
            or (require_refresh_token and not refresh_token)
        ):
            raise SpotifyAdapterError(
                SpotifyErrorKind.INVALID_RESPONSE,
                "Spotify returned an invalid token response",
            )
        scope_value = payload.get("scope")
        if scope_value is None:
            scopes = default_scopes
        elif isinstance(scope_value, str):
            scopes = tuple(dict.fromkeys(scope_value.split()))
        else:
            raise SpotifyAdapterError(
                SpotifyErrorKind.INVALID_RESPONSE,
                "Spotify returned invalid authorization scopes",
            )
        if not scopes:
            raise SpotifyAdapterError(
                SpotifyErrorKind.INVALID_RESPONSE,
                "Spotify returned no authorization scopes",
            )
        return (
            AccessToken(
                value=access_token,
                expires_at=self._now() + timedelta(seconds=float(expires_in)),
                scopes=scopes,
            ),
            refresh_token,
        )

    def _warn_extra_scopes(self, scopes: tuple[str, ...]) -> None:
        if set(scopes) - set(REQUIRED_SCOPES):
            self._warning_sink("Spotify granted unexpected extra scopes; they will not be used")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": " ".join(REQUIRED_SCOPES),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _safe_json_mapping(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise SpotifyAdapterError(
            SpotifyErrorKind.INVALID_RESPONSE,
            "Spotify returned an invalid response",
        ) from None
    if not isinstance(payload, dict):
        raise SpotifyAdapterError(
            SpotifyErrorKind.INVALID_RESPONSE,
            "Spotify returned an invalid response",
        )
    return payload
