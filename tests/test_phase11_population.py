from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

import _bootstrap  # noqa:F401

from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.adapters.spotify.population import SpotifyManagedTrackAdder
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, CanonicalTrackIdentityLink, IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.ports.music_platform import PlatformAccountIdentity, PlatformPlaylistSummary, TrackIdentityPage
from spotify_playlist_taxonomy.workflows.phase11 import Phase11Service, PopulationError, PopulationRecoveryRequired

ROOT = Path(__file__).resolve().parents[1]


class _Reader:
    def __init__(self, tracks: tuple[PlatformTrackIdentity, ...] = (), *, public: bool | None = False, owner: str = "user.synthetic", collaborative: bool = False, broken: bool = False) -> None:
        self.tracks, self.public, self.owner, self.collaborative, self.broken, self.reads = tracks, public, owner, collaborative, broken, 0
    def read_current_account(self) -> PlatformAccountIdentity: return PlatformAccountIdentity("account.synthetic", "user.synthetic")
    def read_playlist(self, playlist_id: str) -> PlatformPlaylistSummary: return PlatformPlaylistSummary(playlist_id, "Chill", self.owner, self.collaborative, self.public, len(self.tracks), "synthetic")
    def read_playlist_items(self, playlist_id: str, cursor: str | None = None) -> TrackIdentityPage:
        self.reads += 1
        if self.broken: raise RuntimeError("synthetic read failure")
        entries = tuple(__import__("spotify_playlist_taxonomy.ports.music_platform", fromlist=["PlatformTrackReadEntry"]).PlatformTrackReadEntry(index, identity=item) for index, item in enumerate(self.tracks))
        return TrackIdentityPage(self.tracks, None, None, entries=entries, limit=len(entries), total=len(entries))


class _Adder:
    def __init__(self, reader: _Reader, *, error: SpotifyErrorKind | None = None, count_after: int = 1) -> None:
        self.reader, self.error, self.count_after, self.calls = reader, error, count_after, []
    def add_one_managed_track(self, playlist_id: str, provider_track_id: str) -> None:
        self.calls.append((playlist_id, provider_track_id))
        if self.error is not None: raise SpotifyAdapterError(self.error, f"{self.error.value}: synthetic")
        self.reader.tracks = tuple(PlatformTrackIdentity(f"seen.{i}", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), provider_track_id) for i in range(self.count_after))


class Phase11PopulationTests(unittest.TestCase):
    target, canonical, binding = "output.vibe.chill", "canonical.synthetic.phase11", "binding.synthetic"
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection); self.bundle = load_phase4_bundle(ROOT / "config" / "application.yaml"); sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.connection.execute("INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic', 'user.synthetic', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)", (self.binding,))
        self.connection.execute("INSERT INTO managed_target_registrations VALUES (?, 'registration.synthetic', 'spotify', ?, 'playlistsynthetic', 'active', 'phase10_create', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)", (self.target, self.binding))
        self.connection.commit()
        source, scan = "source.synthetic.phase11", "scan.synthetic.phase11"
        phase3 = Phase3Store(self.connection); phase3.register_liked_source(source, self.binding)
        phase3.start_scan(scan_id=scan, source_id=source, binding_id=self.binding, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")
        store = SQLiteStateStore(self.connection); store.save_canonical_track(CanonicalTrack(self.canonical, "Synthetic", ("Artist",)))
        store.save_track_identity(PlatformTrackIdentity("identity.synthetic", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), "tracksynthetic"))
        self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, 'identity.synthetic', ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, scan, source))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES ('link.synthetic', ?, 'identity.synthetic', ?, '2026-01-01T00:00:00Z')", (self.binding, self.canonical)); self.connection.commit()
        spec = next(item for item in self.bundle.specifications if item.logical_playlist_id == "vibe.chill")
        self.connection.execute("INSERT INTO phase6_proposals VALUES ('proposal.synthetic', ?, 'vibe.chill', 'include', 'supported', .9, ?, ?, ?, 'synthetic.v1', 'input', ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')", (self.canonical, spec.specification_version, self.bundle.taxonomy.taxonomy_version, self.bundle.vocabulary.vocabulary_version, self.bundle.digest)); self.connection.commit()
        self.service = Phase11Service(self.connection, self.bundle)
    def tearDown(self) -> None: self.connection.close(); self.temp.cleanup()
    def _args(self, reader: _Reader): return dict(account_binding_id=self.binding, account_id="account.synthetic", scopes=("playlist-modify-private",), reader=reader)

    def _additional_desired_membership(self, suffix: str, target: str = "output.activity.driving") -> str:
        canonical = f"canonical.synthetic.phase11.{suffix}"
        identity = f"identity.synthetic.phase11.{suffix}"
        SQLiteStateStore(self.connection).save_canonical_track(CanonicalTrack(canonical, "Synthetic", ("Artist",)))
        self.connection.execute(
            "INSERT INTO managed_target_registrations VALUES (?, ?, 'spotify', ?, ?, 'active', 'phase10_create', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)",
            (target, f"registration.synthetic.{suffix}", self.binding, f"playlist{suffix}"),
        )
        self.connection.commit()
        SQLiteStateStore(self.connection).save_track_identity(PlatformTrackIdentity(identity, IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), f"track{suffix}"))
        self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, ?, 'scan.synthetic.phase11', 'source.synthetic.phase11', NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, identity))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')", (f"link.synthetic.phase11.{suffix}", self.binding, identity, canonical))
        semantic_id = target.removeprefix("output.")
        spec = next(item for item in self.bundle.specifications if item.logical_playlist_id == semantic_id)
        self.connection.execute("INSERT INTO phase6_proposals VALUES (?, ?, ?, 'include', 'supported', .9, ?, ?, ?, 'synthetic.v1', 'input', ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')", (f"proposal.synthetic.phase11.{suffix}", canonical, semantic_id, spec.specification_version, self.bundle.taxonomy.taxonomy_version, self.bundle.vocabulary.vocabulary_version, self.bundle.digest))
        self.connection.commit()
        return canonical

    def test_preview_and_apply_are_one_track_idempotent(self) -> None:
        reader = _Reader(); preview = self.service.preview(self.target, self.canonical, **self._args(reader)); self.assertEqual("would_add", preview.state)
        adder = _Adder(reader); result = self.service.apply(self.target, self.canonical, f"ADD:{self.target}:{self.canonical}", adder=adder, **self._args(reader))
        self.assertEqual("confirmed_added", result.state); self.assertEqual([("playlistsynthetic", "tracksynthetic")], adder.calls)
        self.assertEqual("already_present", self.service.apply(self.target, self.canonical, f"ADD:{self.target}:{self.canonical}", adder=adder, **self._args(reader)).state); self.assertEqual(1, len(adder.calls))

    def test_blocks_non_desired_mapping_policy_account_and_confirmation(self) -> None:
        reader = _Reader()
        with self.assertRaises(PopulationError): self.service.apply(self.target, self.canonical, "wrong", adder=_Adder(reader), **self._args(reader))
        self.connection.execute("DELETE FROM phase6_proposals"); self.connection.commit(); self.assertEqual("blocked", self.service.preview(self.target, self.canonical, **self._args(reader)).state)
        self.connection.execute("UPDATE managed_target_registrations SET policy_confirmed=0"); self.connection.commit(); self.assertEqual("blocked", self.service.preview(self.target, self.canonical, **self._args(reader)).state)

    def test_relinking_and_scope_are_safe(self) -> None:
        relinked = PlatformTrackIdentity("seen", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), "replacement", linked_from_provider_track_id="tracksynthetic")
        self.assertEqual("already_present", self.service.preview(self.target, self.canonical, **self._args(_Reader((relinked,)))).state)
        self.assertEqual("blocked", self.service.preview(self.target, self.canonical, **dict(self._args(_Reader(public=True)), scopes=("playlist-modify-private",))).state)
        self.assertEqual("tracksynthetic", self.connection.execute("SELECT provider_track_id FROM track_identities WHERE identity_id='identity.synthetic'").fetchone()[0])

    def test_rejection_and_ambiguous_readback_require_no_blind_retry(self) -> None:
        reader = _Reader()
        with self.assertRaises(PopulationError): self.service.apply(self.target, self.canonical, f"ADD:{self.target}:{self.canonical}", adder=_Adder(reader, error=SpotifyErrorKind.PROVIDER_REJECTED), **self._args(reader))
        with self.assertRaises(PopulationRecoveryRequired): self.service.apply(self.target, self.canonical, f"ADD:{self.target}:{self.canonical}", adder=_Adder(reader, count_after=0), **self._args(reader))
        self.assertEqual("recovery_required", self.service.preview(self.target, self.canonical, **self._args(reader)).state)
        recovery = self.service.recover_verify(self.target, self.canonical, f"RECOVER:{self.target}:{self.canonical}", **self._args(reader)); self.assertEqual("recovery_required", recovery.state)

    def test_batch_preview_apply_revalidates_and_preserves_one_track_journaling(self) -> None:
        second = self._additional_desired_membership("second")
        reader = _Reader()
        preview = self.service.batch_preview((self.canonical, second), **self._args(reader))
        self.assertEqual(2, preview.desired_memberships)
        self.assertEqual(2, preview.count("would_add"))
        with self.assertRaises(PopulationError):
            self.service.batch_apply((self.canonical, second), "APPLY:wrong", adder=_Adder(reader), **self._args(reader))
        class AccumulatingAdder:
            def __init__(self, target: _Reader) -> None: self.target, self.calls = target, []
            def add_one_managed_track(self, playlist_id: str, provider_track_id: str) -> None:
                self.calls.append((playlist_id, provider_track_id))
                self.target.tracks += (PlatformTrackIdentity(f"seen.{provider_track_id}", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), provider_track_id),)
        adder = AccumulatingAdder(reader)
        outcome = self.service.batch_apply((second, self.canonical), f"APPLY:{preview.plan_id}", adder=adder, **self._args(reader))
        self.assertEqual(2, outcome.count("confirmed_added"))
        self.assertEqual(2, len(adder.calls))
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM managed_target_addition_attempts WHERE state='confirmed_added'").fetchone()[0])
        rerun = self.service.batch_preview((self.canonical, second), **self._args(reader))
        self.assertEqual(2, rerun.count("already_present"))

    def test_batch_skips_blocked_and_stops_after_ambiguous_outbound_result(self) -> None:
        second = self._additional_desired_membership("ambiguous")
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE canonical_track_id=?", (second,)); self.connection.commit()
        preview = self.service.batch_preview((self.canonical, second), **self._args(_Reader()))
        self.assertEqual(1, preview.count("would_add")); self.assertEqual(1, preview.count("blocked"))
        self._additional_desired_membership("third", "output.activity.gym")
        third = "canonical.synthetic.phase11.third"
        outcome = self.service.batch_apply((self.canonical, second, third), f"APPLY:{self.service.batch_plan_id((self.canonical, second, third))}", adder=_Adder(_Reader(), count_after=0), **self._args(_Reader()))
        self.assertEqual(1, outcome.count("recovery_required"))
        self.assertEqual(1, outcome.count("not_attempted"))
        self.assertEqual(1, outcome.count("blocked"))

    def test_adapter_posts_one_uri_without_position_and_no_generic_writer_exists(self) -> None:
        seen: dict[str, object] = {}
        class Token:
            def access_token(self, *, force_refresh: bool = False) -> str: return "synthetic"
        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"], seen["content"] = request.url.path, request.content; return httpx.Response(201, json={"snapshot_id": "snapshot"})
        adapter = SpotifyManagedTrackAdder(token_provider=Token(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        adapter.add_one_managed_track("playlistsynthetic", "tracksynthetic")
        self.assertEqual("/v1/playlists/playlistsynthetic/items", seen["path"]); self.assertEqual(b'{"uris":["spotify:track:tracksynthetic"]}', seen["content"])
        from spotify_playlist_taxonomy.ports import music_platform
        self.assertFalse(hasattr(music_platform, "GuardedMusicPlatformWriter")); self.assertFalse(hasattr(adapter, "remove_items")); self.assertFalse(hasattr(adapter, "add_items"))


if __name__ == "__main__": unittest.main()
