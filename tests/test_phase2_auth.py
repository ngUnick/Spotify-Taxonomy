from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
import httpx

from spotify_playlist_taxonomy.adapters.spotify.auth import (
    REQUIRED_SCOPES,
    SpotifyAuthManager,
    _pkce_challenge,
    mask_identifier,
    resolve_client_id,
)
from spotify_playlist_taxonomy.adapters.spotify.callback import CallbackResult
from spotify_playlist_taxonomy.adapters.spotify.credentials import CredentialEnvelope
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind

NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
CLIENT_ID = "synthetic-client-id-123456"


class FakeStore:
    def __init__(self, credential: CredentialEnvelope | None = None) -> None:
        self.credential = credential
        self.deleted = False

    def load(self, client_id: str) -> CredentialEnvelope | None:
        self.last_client_id = client_id
        return self.credential

    def save(self, client_id: str, credential: CredentialEnvelope) -> None:
        self.last_client_id = client_id
        self.credential = credential

    def delete(self, client_id: str) -> bool:
        self.last_client_id = client_id
        existed = self.credential is not None
        self.credential = None
        self.deleted = True
        return existed


class FakeCallback:
    redirect_uri = "http://127.0.0.1:54321/callback"

    def __init__(self, result: CallbackResult) -> None:
        self.result = result
        self.closed = False

    def wait(self, timeout_seconds: float) -> CallbackResult:
        self.timeout_seconds = timeout_seconds
        return self.result

    def close(self) -> None:
        self.closed = True


class AuthenticationTests(unittest.TestCase):
    def test_exact_required_scopes(self) -> None:
        self.assertEqual(
            (
                "user-read-private",
                "playlist-read-private",
                "playlist-read-collaborative",
                "user-library-read",
            ),
            REQUIRED_SCOPES,
        )
        self.assertNotIn("user-read-email", REQUIRED_SCOPES)
        self.assertFalse(any("modify" in scope for scope in REQUIRED_SCOPES))

    def test_client_id_precedence_and_masking(self) -> None:
        environment = {"SPOTIFY_CLIENT_ID": "environment-client"}
        self.assertEqual("cli-client", resolve_client_id("cli-client", environment))
        self.assertEqual("environment-client", resolve_client_id(None, environment))
        self.assertEqual("synt…ient", mask_identifier("synthetic-client"))
        with self.assertRaises(SpotifyAdapterError):
            resolve_client_id(None, {})

    def test_no_browser_prints_authorization_url_and_never_verifier(self) -> None:
        store = FakeStore()
        callback = FakeCallback(CallbackResult("synthetic-state", "synthetic-code", None))
        displayed: list[str] = []
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "access_token": "synthetic-access-token",
                "refresh_token": "synthetic-refresh-token",
                "expires_in": 3600,
                "scope": " ".join(REQUIRED_SCOPES),
            })

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID,
                credential_store=store,
                http_client=client,
                callback_timeout_seconds=123,
                callback_factory=lambda: callback,
                browser_open=lambda url: self.fail("browser should not open"),
                authorization_url_sink=displayed.append,
                now=lambda: NOW,
            )
            with patch(
                "spotify_playlist_taxonomy.adapters.spotify.auth.secrets.token_urlsafe",
                side_effect=["synthetic-state", "synthetic-verifier"],
            ):
                token = manager.login(no_browser=True)

        self.assertEqual("synthetic-access-token", token.value)
        self.assertTrue(callback.closed)
        self.assertEqual(123, callback.timeout_seconds)
        self.assertEqual(1, len(displayed))
        query = parse_qs(urlsplit(displayed[0]).query)
        self.assertEqual([CLIENT_ID], query["client_id"])
        self.assertEqual(["synthetic-state"], query["state"])
        self.assertEqual([_pkce_challenge("synthetic-verifier")], query["code_challenge"])
        self.assertNotIn("synthetic-verifier", displayed[0])
        form = parse_qs(requests[0].content.decode())
        self.assertEqual(["synthetic-verifier"], form["code_verifier"])
        self.assertNotIn("client_secret", form)
        self.assertEqual("synthetic-refresh-token", store.credential.refresh_token)

    def test_denial_closes_callback_and_does_not_exchange(self) -> None:
        callback = FakeCallback(CallbackResult("synthetic-state", None, "access_denied"))
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("no POST"))) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID,
                credential_store=FakeStore(),
                http_client=client,
                callback_factory=lambda: callback,
                authorization_url_sink=lambda url: None,
            )
            with patch(
                "spotify_playlist_taxonomy.adapters.spotify.auth.secrets.token_urlsafe",
                side_effect=["synthetic-state", "synthetic-verifier"],
            ):
                with self.assertRaises(SpotifyAdapterError) as raised:
                    manager.login(no_browser=True)
        self.assertEqual(SpotifyErrorKind.AUTHORIZATION_DENIED, raised.exception.kind)
        self.assertTrue(callback.closed)

    def test_state_mismatch_fails_before_exchange_and_closes_callback(self) -> None:
        callback = FakeCallback(CallbackResult("wrong-state", "synthetic-code", None))
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("no POST"))) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID,
                credential_store=FakeStore(),
                http_client=client,
                callback_factory=lambda: callback,
                authorization_url_sink=lambda url: None,
            )
            with patch(
                "spotify_playlist_taxonomy.adapters.spotify.auth.secrets.token_urlsafe",
                side_effect=["expected-state", "synthetic-verifier"],
            ):
                with self.assertRaises(SpotifyAdapterError) as raised:
                    manager.login(no_browser=True)
        self.assertEqual(SpotifyErrorKind.AUTHORIZATION_FAILED, raised.exception.kind)
        self.assertTrue(callback.closed)

    def test_refresh_preserves_then_rotates_refresh_token(self) -> None:
        stored = CredentialEnvelope("refresh-one", REQUIRED_SCOPES, NOW)
        store = FakeStore(stored)
        payloads = [
            {"access_token": "access-one", "expires_in": 3600},
            {"access_token": "access-two", "expires_in": 3600, "refresh_token": "refresh-two"},
        ]
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payloads.pop(0)))
        ) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID, credential_store=store, http_client=client, now=lambda: NOW
            )
            self.assertEqual("access-one", manager.access_token())
            self.assertEqual("refresh-one", store.credential.refresh_token)
            self.assertEqual("access-two", manager.access_token(force_refresh=True))
            self.assertEqual("refresh-two", store.credential.refresh_token)

    def test_cached_token_refreshes_sixty_seconds_early(self) -> None:
        store = FakeStore(CredentialEnvelope("refresh", REQUIRED_SCOPES, NOW))
        responses = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal responses
            responses += 1
            return httpx.Response(200, json={"access_token": f"access-{responses}", "expires_in": 120})

        clock = [NOW]
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID, credential_store=store, http_client=client, now=lambda: clock[0]
            )
            self.assertEqual("access-1", manager.access_token())
            clock[0] = NOW + timedelta(seconds=61)
            self.assertEqual("access-2", manager.access_token())

    def test_invalid_grant_deletes_credential_and_uses_safe_error(self) -> None:
        store = FakeStore(CredentialEnvelope("sensitive-refresh", REQUIRED_SCOPES, NOW))
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"error": "invalid_grant"})
            )
        ) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID, credential_store=store, http_client=client, now=lambda: NOW
            )
            with self.assertRaises(SpotifyAdapterError) as raised:
                manager.access_token()
        self.assertEqual(SpotifyErrorKind.NOT_AUTHORIZED, raised.exception.kind)
        self.assertTrue(store.deleted)
        self.assertNotIn("sensitive-refresh", str(raised.exception))
        self.assertNotIn(CLIENT_ID, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
