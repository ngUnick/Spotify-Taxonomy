from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import ANY, patch

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
from spotify_playlist_taxonomy.adapters.spotify.callback import (
    CALLBACK_HOST,
    DEFAULT_CALLBACK_PORT,
    LoopbackCallbackServer,
)
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
    def __init__(self, result: CallbackResult, port: int = DEFAULT_CALLBACK_PORT) -> None:
        self.result = result
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"
        self.closed = False

    def wait(self, timeout_seconds: float) -> CallbackResult:
        self.timeout_seconds = timeout_seconds
        return self.result

    def close(self) -> None:
        self.closed = True


class AuthenticationTests(unittest.TestCase):
    def test_callback_server_binds_exact_default_and_configured_ports(self) -> None:
        for port in (DEFAULT_CALLBACK_PORT, 9443):
            with self.subTest(port=port), patch(
                "spotify_playlist_taxonomy.adapters.spotify.callback.HTTPServer"
            ) as server_type:
                server_type.return_value.server_port = port
                callback = LoopbackCallbackServer(port)
                server_type.assert_called_once_with((CALLBACK_HOST, port), ANY)
                self.assertEqual(f"http://127.0.0.1:{port}/callback", callback.redirect_uri)
                callback.close()

    def test_callback_bind_failure_has_no_port_fallback(self) -> None:
        with patch(
            "spotify_playlist_taxonomy.adapters.spotify.callback.HTTPServer",
            side_effect=OSError("synthetic occupied port"),
        ) as server_type, self.assertRaises(SpotifyAdapterError) as raised:
            LoopbackCallbackServer(8888)
        self.assertEqual(SpotifyErrorKind.CONFIGURATION, raised.exception.kind)
        self.assertIn("configured port 8888", str(raised.exception))
        self.assertIn("registered", str(raised.exception))
        server_type.assert_called_once_with((CALLBACK_HOST, 8888), ANY)

    def test_callback_port_validation_fails_closed(self) -> None:
        for invalid in (0, 1023, 65536, True):
            with self.subTest(port=invalid), self.assertRaises(SpotifyAdapterError):
                LoopbackCallbackServer(invalid)

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
        callback_ports: list[int] = []

        def callback_factory(port: int) -> FakeCallback:
            callback_ports.append(port)
            return callback

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
                callback_factory=callback_factory,
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
        self.assertEqual([DEFAULT_CALLBACK_PORT], callback_ports)
        self.assertEqual(1, len(displayed))
        query = parse_qs(urlsplit(displayed[0]).query)
        self.assertEqual([CLIENT_ID], query["client_id"])
        self.assertEqual(["synthetic-state"], query["state"])
        self.assertEqual([_pkce_challenge("synthetic-verifier")], query["code_challenge"])
        self.assertEqual([callback.redirect_uri], query["redirect_uri"])
        self.assertNotIn("synthetic-verifier", displayed[0])
        form = parse_qs(requests[0].content.decode())
        self.assertEqual(["synthetic-verifier"], form["code_verifier"])
        self.assertEqual([callback.redirect_uri], form["redirect_uri"])
        self.assertNotIn("client_secret", form)
        self.assertEqual("synthetic-refresh-token", store.credential.refresh_token)

    def test_denial_closes_callback_and_does_not_exchange(self) -> None:
        callback = FakeCallback(CallbackResult("synthetic-state", None, "access_denied"))
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("no POST"))) as client:
            manager = SpotifyAuthManager(
                client_id=CLIENT_ID,
                credential_store=FakeStore(),
                http_client=client,
                callback_factory=lambda port: callback,
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
                callback_factory=lambda port: callback,
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
