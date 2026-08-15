from datetime import datetime, timedelta, timezone
import unittest

import _bootstrap  # noqa: F401
import httpx

from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.adapters.spotify.reader import SpotifyReadOnlyAdapter
from spotify_playlist_taxonomy.ports.music_platform import PlatformReadIssueKind


class FakeTokens:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "synthetic-refreshed" if force_refresh else "synthetic-access"


def track(track_id: str, name: str = "Invented Song") -> dict[str, object]:
    return {
        "id": track_id,
        "name": name,
        "type": "track",
        "is_local": False,
        "artists": [{"name": "Synthetic Artist"}],
    }


class ReaderTests(unittest.TestCase):
    def make_reader(self, handler, **kwargs):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        tokens = FakeTokens()
        reader = SpotifyReadOnlyAdapter(
            token_provider=tokens, http_client=client, **kwargs
        )
        self.addCleanup(client.close)
        return reader, tokens

    def test_current_account_projects_only_stable_and_user_ids(self) -> None:
        response = {
            "account_id": "synthetic-account",
            "id": "synthetic-user",
            "display_name": "Must be discarded",
            "email": "discarded@example.invalid",
        }
        reader, _ = self.make_reader(lambda request: httpx.Response(200, json=response))
        account = reader.read_current_account()
        self.assertEqual("synthetic-account", account.account_id)
        self.assertEqual("synthetic-user", account.spotify_user_id)
        self.assertFalse(hasattr(account, "display_name"))

    def test_adapter_exposes_no_writer_or_generic_request_surface(self) -> None:
        reader, _ = self.make_reader(
            lambda request: httpx.Response(200, json={"account_id": "A", "id": "U"})
        )
        self.assertFalse(hasattr(reader, "add_items"))
        self.assertFalse(hasattr(reader, "create_managed_playlist"))
        self.assertFalse(hasattr(reader, "request"))

    def test_playlist_nullability_is_preserved(self) -> None:
        payload = {
            "items": [{
                "id": "PlaylistOne",
                "name": "Synthetic Source",
                "owner": {"id": "synthetic-owner"},
                "collaborative": False,
                "public": None,
                "snapshot_id": "synthetic-snapshot",
            }],
            "next": None,
        }
        reader, _ = self.make_reader(lambda request: httpx.Response(200, json=payload))
        item = reader.read_current_user_playlists().items[0]
        self.assertIsNone(item.public)
        self.assertIsNone(item.item_count)
        self.assertNotEqual(False, item.public)
        self.assertNotEqual(0, item.item_count)

    def test_playlist_item_parser_prefers_canonical_and_isolates_legacy(self) -> None:
        payload = {
            "items": [
                {"is_local": False, "item": track("CanonicalOne")},
                {
                    "item": track("CanonicalWins"),
                    "track": track("LegacyIgnored"),
                },
                {"track": track("LegacyOnly")},
                {"item": None, "track": track("MustNotFallback")},
            ],
            "next": None,
            "offset": 0,
            "limit": 50,
            "total": 4,
        }
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=payload)

        reader, _ = self.make_reader(handler)
        page = reader.read_playlist_items("SyntheticPlaylist")
        self.assertEqual(
            ["CanonicalOne", "CanonicalWins", "LegacyOnly"],
            [item.provider_track_id for item in page.items],
        )
        self.assertEqual((PlatformReadIssueKind.NULL,), tuple(issue.kind for issue in page.issues))
        self.assertEqual(3, page.schema_diagnostics.canonical_item_count)
        self.assertEqual(1, page.schema_diagnostics.legacy_track_count)
        self.assertEqual("/v1/playlists/SyntheticPlaylist/items", requests[0].url.path)
        self.assertNotIn("/tracks", requests[0].url.path)
        self.assertIn("item(", requests[0].url.params["fields"])
        self.assertIn("track(", requests[0].url.params["fields"])

    def test_local_unsupported_unavailable_and_malformed_items_are_issues(self) -> None:
        payload = {
            "items": [
                {"item": {**track("Local"), "is_local": True}},
                {"item": {"id": "Episode", "name": "Episode", "type": "episode"}},
                {"item": {**track("Missing"), "id": None}},
                {"item": {**track("BadArtists"), "artists": []}},
                "not-an-object",
            ],
            "next": None,
        }
        reader, _ = self.make_reader(lambda request: httpx.Response(200, json=payload))
        page = reader.read_playlist_items("SyntheticPlaylist")
        self.assertFalse(page.items)
        self.assertEqual(
            [
                PlatformReadIssueKind.LOCAL,
                PlatformReadIssueKind.UNSUPPORTED,
                PlatformReadIssueKind.UNAVAILABLE,
                PlatformReadIssueKind.MALFORMED,
                PlatformReadIssueKind.MALFORMED,
            ],
            [issue.kind for issue in page.issues],
        )

    def test_saved_tracks_use_saved_track_object_without_schema_diagnostics(self) -> None:
        payload = {"items": [{"track": track("SavedOne")}], "next": None}
        reader, _ = self.make_reader(lambda request: httpx.Response(200, json=payload))
        page = reader.read_saved_tracks()
        self.assertEqual("SavedOne", page.items[0].provider_track_id)
        self.assertIsNone(page.schema_diagnostics)

    def test_cursor_is_local_and_arbitrary_next_url_is_never_followed(self) -> None:
        requests: list[httpx.Request] = []
        responses = [
            {"items": [{"track": track("SavedOne")}], "next": "https://evil.invalid/path"},
            {"items": [], "next": None},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=responses.pop(0))

        reader, _ = self.make_reader(handler)
        first = reader.read_saved_tracks()
        reader.read_saved_tracks(first.next_cursor)
        self.assertEqual(["api.spotify.com", "api.spotify.com"], [r.url.host for r in requests])
        self.assertEqual("1", requests[1].url.params["offset"])
        with self.assertRaises(SpotifyAdapterError) as raised:
            reader.read_saved_tracks("not-a-cursor")
        self.assertEqual(SpotifyErrorKind.INVALID_CURSOR, raised.exception.kind)

    def test_401_forces_exactly_one_refresh_and_replays(self) -> None:
        responses = [httpx.Response(401), httpx.Response(200, json={"account_id": "A", "id": "U"})]
        reader, tokens = self.make_reader(lambda request: responses.pop(0))
        reader.read_current_account()
        self.assertEqual([False, True, False], tokens.calls)

    def test_second_401_stops_after_single_refresh(self) -> None:
        responses = [httpx.Response(401), httpx.Response(401)]
        reader, tokens = self.make_reader(lambda request: responses.pop(0))
        with self.assertRaises(SpotifyAdapterError) as raised:
            reader.read_current_account()
        self.assertEqual(SpotifyErrorKind.NOT_AUTHORIZED, raised.exception.kind)
        self.assertEqual([False, True, False], tokens.calls)

    def test_transport_error_does_not_retain_sensitive_cause(self) -> None:
        sensitive = "synthetic-sensitive-request-detail"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(sensitive, request=request)

        reader, _ = self.make_reader(handler, max_read_attempts=1)
        with self.assertRaises(SpotifyAdapterError) as raised:
            reader.read_playlist("SyntheticPlaylist")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(sensitive, str(raised.exception))

    def test_transient_retry_and_valid_retry_after(self) -> None:
        sleeps: list[float] = []
        responses = [
            httpx.Response(503),
            httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"reason": "RATE_LIMIT"}}),
            httpx.Response(200, json={"account_id": "A", "id": "U"}),
        ]
        reader, _ = self.make_reader(
            lambda request: responses.pop(0), max_read_attempts=3, sleeper=sleeps.append
        )
        reader.read_current_account()
        self.assertEqual([1.0, 3.0], sleeps)

    def test_invalid_retry_after_values_use_bounded_fallback(self) -> None:
        for header in (None, "not-a-number", "-1", "nan", "1e3", "3.5", "+3", "999999"):
            with self.subTest(header=header):
                sleeps: list[float] = []
                responses = [
                    httpx.Response(429, headers={} if header is None else {"Retry-After": header}),
                    httpx.Response(200, json={"account_id": "A", "id": "U"}),
                ]
                reader, _ = self.make_reader(
                    lambda request: responses.pop(0), max_read_attempts=2, sleeper=sleeps.append
                )
                reader.read_current_account()
                self.assertEqual([1.0], sleeps)

    def test_retry_after_http_date_is_supported(self) -> None:
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        sleeps: list[float] = []
        responses = [
            httpx.Response(429, headers={"Retry-After": "Fri, 14 Aug 2026 12:00:05 GMT"}),
            httpx.Response(200, json={"account_id": "A", "id": "U"}),
        ]
        reader, _ = self.make_reader(
            lambda request: responses.pop(0),
            max_read_attempts=2,
            sleeper=sleeps.append,
            now=lambda: now,
        )
        reader.read_current_account()
        self.assertEqual([5.0], sleeps)

    def test_quota_exhaustion_never_retries(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, json={"error": {"reason": "QUOTA_EXCEEDED"}})

        reader, _ = self.make_reader(handler, max_read_attempts=3, sleeper=sleeps.append)
        with self.assertRaises(SpotifyAdapterError) as raised:
            reader.read_current_account()
        self.assertEqual(SpotifyErrorKind.QUOTA_EXHAUSTED, raised.exception.kind)
        self.assertEqual(1, calls)
        self.assertFalse(sleeps)

    def test_errors_do_not_include_response_data_or_identifiers(self) -> None:
        sensitive = "synthetic-sensitive-response-body"
        reader, _ = self.make_reader(
            lambda request: httpx.Response(403, text=sensitive)
        )
        with self.assertRaises(SpotifyAdapterError) as raised:
            reader.read_playlist("SyntheticPlaylist")
        self.assertNotIn(sensitive, str(raised.exception))
        self.assertNotIn("SyntheticPlaylist", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
