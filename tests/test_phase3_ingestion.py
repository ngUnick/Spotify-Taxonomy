from __future__ import annotations

from collections import deque
from pathlib import Path
import sqlite3
import tempfile
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.adapters.spotify.errors import (
    SpotifyAdapterError,
    SpotifyErrorKind,
)
from spotify_playlist_taxonomy.domain.ingestion import (
    InboxKind,
    MatchCandidateState,
    OwnershipState,
    ScanStatus,
    StagedSourceEntry,
)
from spotify_playlist_taxonomy.domain.models import (
    IdentityProvider,
    PlatformTrackIdentity,
    PlaylistRole,
)
from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database
from spotify_playlist_taxonomy.persistence.phase3 import (
    AccountMismatchError,
    Phase3StateError,
    Phase3Store,
)
from spotify_playlist_taxonomy.ports.music_platform import (
    PlatformAccountIdentity,
    PlatformPlaylistSummary,
    PlatformTrackReadEntry,
    PlaylistSummaryPage,
    TrackIdentityPage,
)
from spotify_playlist_taxonomy.workflows.phase3 import DIGEST_VERSION, Phase3Coordinator
from spotify_playlist_taxonomy.workflows.review import Phase3ReviewService


def identity(
    track_id: str,
    title: str = "Invented Song",
    artist: str = "Synthetic Artist",
    *,
    version: str | None = None,
) -> PlatformTrackIdentity:
    return PlatformTrackIdentity(
        identity_id=f"spotify.{track_id}",
        provider=IdentityProvider.SPOTIFY,
        provider_track_id=track_id,
        title=title,
        artists=(artist,),
        version_label=version,
    )


def summary(
    snapshot: str | None = "snapshot-1",
    *,
    playlist_id: str = "playlist-1",
    owner: str = "user-1",
    collaborative: bool = False,
) -> PlatformPlaylistSummary:
    return PlatformPlaylistSummary(
        playlist_id=playlist_id,
        name="Synthetic Source",
        owner_user_id=owner,
        collaborative=collaborative,
        public=None,
        item_count=2,
        snapshot_id=snapshot,
    )


def page(
    offset: int,
    values: tuple[PlatformTrackIdentity, ...],
    next_cursor: str | None = None,
    *,
    total: int | None = None,
) -> TrackIdentityPage:
    entries = tuple(
        PlatformTrackReadEntry(item_index=index, identity=value)
        for index, value in enumerate(values)
    )
    return TrackIdentityPage(
        items=values,
        next_cursor=next_cursor,
        snapshot_id=None,
        entries=entries,
        offset=offset,
        limit=len(values),
        total=total,
    )


class FakeReader:
    def __init__(self) -> None:
        self.account = PlatformAccountIdentity("account-1", "user-1")
        self.playlist_summaries: deque[PlatformPlaylistSummary | Exception] = deque([summary()])
        self.playlist_pages: dict[str | None, TrackIdentityPage | Exception] = {}
        self.saved_pages: dict[str | None, deque[TrackIdentityPage | Exception]] = {}
        self.discovered = PlaylistSummaryPage((), None)
        self.playlist_item_cursors: list[str | None] = []
        self.saved_cursors: list[str | None] = []

    def read_current_account(self) -> PlatformAccountIdentity:
        return self.account

    def read_current_user_playlists(self, cursor: str | None = None) -> PlaylistSummaryPage:
        return self.discovered

    def read_playlist(self, playlist_id: str) -> PlatformPlaylistSummary:
        value = (
            self.playlist_summaries.popleft()
            if len(self.playlist_summaries) > 1
            else self.playlist_summaries[0]
        )
        if isinstance(value, Exception):
            raise value
        return value

    def read_playlist_items(
        self, playlist_id: str, cursor: str | None = None
    ) -> TrackIdentityPage:
        self.playlist_item_cursors.append(cursor)
        value = self.playlist_pages[cursor]
        if isinstance(value, Exception):
            raise value
        return value

    def read_saved_tracks(self, cursor: str | None = None) -> TrackIdentityPage:
        self.saved_cursors.append(cursor)
        values = self.saved_pages[cursor]
        value = values.popleft() if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-{self.value}"


class Phase3TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "phase3.db")
        apply_migrations(self.connection)
        self.store = Phase3Store(self.connection)
        self.reader = FakeReader()
        self.ids = SequentialIds()
        self.coordinator = Phase3Coordinator(
            reader=self.reader, store=self.store, id_factory=self.ids
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def binding_id(self) -> str:
        binding = self.store.active_account()
        assert binding is not None
        return binding.binding_id

    def bind_and_register_playlist(self, playlist_id: str = "playlist-1") -> str:
        self.coordinator.bind_account()
        return self.coordinator.register_playlist(playlist_id, role=PlaylistRole.SOURCE)

    def start(
        self,
        scan_id: str,
        source_id: str,
        *,
        snapshot: str | None = "snapshot-1",
        total: int | None = 1,
    ) -> None:
        self.store.start_scan(
            scan_id=scan_id,
            source_id=source_id,
            binding_id=self.binding_id(),
            starting_snapshot_id=snapshot,
            starting_total=total,
            initial_page_signature=None,
            digest_version=DIGEST_VERSION,
        )

    def stage(
        self,
        scan_id: str,
        entries: tuple[StagedSourceEntry, ...],
        *,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> None:
        self.store.stage_page(
            scan_id=scan_id,
            binding_id=self.binding_id(),
            entries=entries,
            next_cursor=cursor,
            next_offset=(len(entries) if offset is None else offset),
        )

    def publish_direct(
        self, source_id: str, value: PlatformTrackIdentity, scan_id: str
    ) -> str:
        self.start(scan_id, source_id)
        self.stage(scan_id, (StagedSourceEntry(0, 0, value),), offset=1)
        self.store.publish_scan(scan_id, self.binding_id(), "snapshot-1")
        return value.identity_id


class AccountAndSourceTests(Phase3TestCase):
    def test_account_id_is_binding_key_and_user_id_is_refreshable_metadata(self) -> None:
        self.coordinator.bind_account()
        first = self.store.active_account()
        assert first is not None
        self.reader.account = PlatformAccountIdentity("account-1", "renamed-user")
        self.coordinator.verify_account()
        self.assertEqual(first.binding_id, self.binding_id())
        self.assertEqual("renamed-user", self.store.active_account().current_user_id)  # type: ignore[union-attr]

        self.reader.account = PlatformAccountIdentity("different-account", "user-2")
        with self.assertRaises(AccountMismatchError):
            self.coordinator.verify_account()

    def test_unbind_requires_disabled_sources_and_rebind_is_isolated(self) -> None:
        source_id = self.bind_and_register_playlist()
        first_binding = self.binding_id()
        with self.assertRaisesRegex(Phase3StateError, "disabled"):
            self.store.unbind_account()
        self.store.disable_source(source_id, first_binding)
        self.store.unbind_account()

        self.reader.account = PlatformAccountIdentity("account-2", "user-2")
        self.coordinator.bind_account()
        self.assertNotEqual(first_binding, self.binding_id())
        with self.assertRaisesRegex(Phase3StateError, "different"):
            self.coordinator.scan(source_id)

    def test_discovery_is_ephemeral_and_collaboration_is_not_membership(self) -> None:
        self.coordinator.bind_account()
        item = summary(owner="someone-else", collaborative=True)
        self.reader.discovered = PlaylistSummaryPage((item,), None)
        discovered = self.coordinator.discover_playlists()
        self.assertEqual(OwnershipState.NON_OWNED, discovered[0][1])
        self.assertTrue(discovered[0][0].collaborative)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM platform_playlist_observations"
            ).fetchone()[0],
        )

    def test_liked_songs_is_single_account_scoped_inbox(self) -> None:
        self.coordinator.bind_account()
        source_id = self.coordinator.register_liked()
        source = self.store.source(source_id)
        self.assertEqual(PlaylistRole.INBOX, source.role)
        self.assertEqual(InboxKind.LIKED_SONGS, source.inbox_kind)
        with self.assertRaises(sqlite3.IntegrityError):
            self.coordinator.register_liked()

    def test_relational_account_source_boundaries_reject_cross_account_rows(self) -> None:
        source_id = self.bind_and_register_playlist()
        first = self.binding_id()
        self.start("first-account-scan", source_id)
        self.stage(
            "first-account-scan",
            (StagedSourceEntry(0, 0, identity("first-account-identity")),),
            offset=1,
        )
        registration = self.connection.execute(
            "SELECT playlist_registration_id FROM ingestion_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]
        self.store.disable_source(source_id, first)
        self.store.unbind_account()
        self.reader.account = PlatformAccountIdentity("account-2", "user-2")
        self.coordinator.bind_account()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO ingestion_sources VALUES "
                "('cross-source', ?, 'playlist', ?, 'source', NULL, 'bad', 1, ?, NULL)",
                (self.binding_id(), registration, "2026-01-01T00:00:00+00:00"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO account_track_identity_state VALUES "
                "(?, 'spotify.first-account-identity', 'first-account-scan', ?, "
                "NULL, NULL, 1, 0, '2026-01-01T00:00:00+00:00')",
                (self.binding_id(), source_id),
            )

    def test_forbidden_and_not_found_are_structured_observations(self) -> None:
        source_id = self.bind_and_register_playlist()
        for kind, expected in (
            (SpotifyErrorKind.FORBIDDEN, "forbidden"),
            (SpotifyErrorKind.NOT_FOUND, "not_found"),
        ):
            self.reader.playlist_summaries = deque([
                SpotifyAdapterError(kind, "sanitized synthetic read failure")
            ])
            with self.assertRaises(SpotifyAdapterError):
                self.coordinator.scan(source_id)
            observed = self.connection.execute(
                "SELECT accessibility_state FROM platform_playlist_observations "
                "ORDER BY observation_id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(expected, observed)


class ScanLifecycleTests(Phase3TestCase):
    def test_incomplete_scan_cannot_publish(self) -> None:
        source_id = self.bind_and_register_playlist()
        self.start("incomplete", source_id)
        self.stage(
            "incomplete",
            (StagedSourceEntry(0, 0, identity("one")),),
            cursor="offset:1",
            offset=1,
        )
        with self.assertRaisesRegex(Phase3StateError, "terminal"):
            self.store.publish_scan("incomplete", self.binding_id(), "snapshot-1")

    def test_published_scan_is_immutable_and_cannot_transition(self) -> None:
        source_id = self.bind_and_register_playlist()
        self.publish_direct(source_id, identity("published"), "published-scan")
        with self.assertRaisesRegex(Phase3StateError, "unpublished running"):
            self.stage(
                "published-scan",
                (StagedSourceEntry(1, 1, identity("late")),),
                offset=2,
            )
        with self.assertRaisesRegex(Phase3StateError, "immutable"):
            self.store.mark_scan(
                "published-scan", self.binding_id(), ScanStatus.RUNNING
            )

    def test_failed_stale_and_abandoned_scans_never_become_authoritative(self) -> None:
        source_id = self.bind_and_register_playlist()
        for index, status in enumerate(
            (ScanStatus.FAILED, ScanStatus.STALE, ScanStatus.ABANDONED)
        ):
            scan_id = f"terminal-{index}"
            self.start(scan_id, source_id)
            self.stage(
                scan_id,
                (StagedSourceEntry(0, 0, identity(f"terminal-{index}")),),
                offset=1,
            )
            self.store.mark_scan(scan_id, self.binding_id(), status, "synthetic")
        self.assertEqual((), self.store.published_identity_ids(self.binding_id()))
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM account_canonical_identity_links"
            ).fetchone()[0],
        )

    def test_page_failure_rolls_back_identity_entries_counts_and_cursor(self) -> None:
        source_id = self.bind_and_register_playlist()
        self.start("rollback", source_id, total=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.stage(
                "rollback",
                (
                    StagedSourceEntry(0, 0, identity("rollback-a")),
                    StagedSourceEntry(0, 0, identity("rollback-b")),
                ),
                cursor="offset:2",
                offset=2,
            )
        row = self.store.scan_row("rollback", self.binding_id())
        self.assertEqual((0, 0, 0, None), (
            row["page_count"], row["occurrence_count"], row["next_offset"], row["resume_cursor"]
        ))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM source_scan_entries").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM track_identities").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM account_track_identity_state").fetchone()[0])

    def test_global_positions_and_digest_ignore_page_boundaries(self) -> None:
        source_id = self.bind_and_register_playlist()
        values = (identity("one"), identity("two"), identity("three"))
        self.start("one-page", source_id, snapshot="s1", total=3)
        self.stage(
            "one-page",
            tuple(StagedSourceEntry(i, 0, value) for i, value in enumerate(values)),
            offset=3,
        )
        digest_a = self.store.publish_scan("one-page", self.binding_id(), "s1")

        self.start("two-pages", source_id, snapshot="s2", total=3)
        self.stage(
            "two-pages",
            (StagedSourceEntry(0, 0, values[0]), StagedSourceEntry(1, 0, values[1])),
            cursor="offset:2",
            offset=2,
        )
        self.stage("two-pages", (StagedSourceEntry(2, 1, values[2]),), offset=3)
        digest_b = self.store.publish_scan("two-pages", self.binding_id(), "s2")
        self.assertEqual(digest_a, digest_b)
        self.assertEqual(DIGEST_VERSION, self.store.scan_row("two-pages")["digest_version"])

    def test_source_publication_cannot_reference_another_sources_scan(self) -> None:
        first = self.bind_and_register_playlist()
        self.reader.playlist_summaries = deque([summary(playlist_id="playlist-2")])
        second = self.coordinator.register_playlist("playlist-2", role=PlaylistRole.SOURCE)
        self.publish_direct(first, identity("first"), "scan-first")
        self.publish_direct(second, identity("second"), "scan-second")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE source_publications SET scan_id = 'scan-second' WHERE source_id = ?",
                (first,),
            )

    def test_same_snapshot_resume_uses_cursor_and_clears_failure(self) -> None:
        source_id = self.bind_and_register_playlist()
        self.start("resume", source_id, total=2)
        self.stage(
            "resume",
            (StagedSourceEntry(0, 0, identity("first-page")),),
            cursor="offset:1",
            offset=1,
        )
        self.store.mark_scan("resume", self.binding_id(), ScanStatus.FAILED, "temporary")
        self.reader.playlist_summaries = deque([summary(), summary()])
        self.reader.playlist_pages = {
            "offset:1": page(1, (identity("second-page"),), total=2)
        }
        result = self.coordinator.scan(source_id, resume=True)
        self.assertEqual("resume", result)
        self.assertEqual(["offset:1"], self.reader.playlist_item_cursors)
        row = self.store.scan_row(result, self.binding_id())
        self.assertEqual(ScanStatus.COMPLETED.value, row["status"])
        self.assertIsNone(row["failure_kind"])

    def test_changed_start_snapshot_abandons_cursor_and_changed_end_blocks_publish(self) -> None:
        source_id = self.bind_and_register_playlist()
        self.start("old", source_id, snapshot="old", total=2)
        self.stage(
            "old", (StagedSourceEntry(0, 0, identity("old")),),
            cursor="offset:1", offset=1,
        )
        self.reader.playlist_summaries = deque([summary("new"), summary("changed-again")])
        self.reader.playlist_pages = {None: page(0, (identity("fresh"),), total=1)}
        new_scan = self.coordinator.scan(source_id, resume=True)
        self.assertEqual(ScanStatus.STALE.value, self.store.scan_row("old")["status"])
        self.assertEqual([None], self.reader.playlist_item_cursors)
        self.assertEqual(ScanStatus.STALE.value, self.store.scan_row(new_scan)["status"])
        self.assertIsNone(self.store.current_publication(source_id, self.binding_id()))

    def test_saved_tracks_failure_restarts_from_zero_and_guard_change_is_stale(self) -> None:
        self.coordinator.bind_account()
        source_id = self.coordinator.register_liked()
        first = page(0, (identity("liked-1"),), "offset:1", total=2)
        self.reader.saved_pages = {
            None: deque([first]),
            "offset:1": deque([RuntimeError("synthetic page failure")]),
        }
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            self.coordinator.scan(source_id)
        failed_scan = self.store.scan_rows(self.binding_id())[0]
        self.assertEqual(ScanStatus.FAILED.value, failed_scan["status"])

        complete = page(0, (identity("liked-2"),), total=1)
        changed = page(0, (identity("liked-3"),), total=1)
        self.reader.saved_pages = {None: deque([complete, changed])}
        second = self.coordinator.scan(source_id)
        self.assertEqual([None, "offset:1", None, None], self.reader.saved_cursors)
        self.assertEqual(ScanStatus.ABANDONED.value, self.store.scan_row(failed_scan["scan_id"])["status"])
        self.assertEqual(ScanStatus.STALE.value, self.store.scan_row(second)["status"])


class MatchingTests(Phase3TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_id = self.bind_and_register_playlist()

    def publish(self, value: PlatformTrackIdentity, scan_id: str) -> str:
        return self.publish_direct(self.source_id, value, scan_id)

    def reconcile(self, identity_id: str, suffix: str = "1") -> str:
        return self.store.reconcile_published_identity(
            self.binding_id(), identity_id,
            matcher_version="test.v1",
            candidate_id=f"candidate-{suffix}", canonical_id=f"canonical-{suffix}",
            link_id=f"link-{suffix}", event_id=f"event-{suffix}",
        )

    def test_untouched_identity_bootstraps_and_active_link_is_reused(self) -> None:
        identity_id = self.publish(identity("bootstrap"), "scan-bootstrap")
        canonical_id = self.reconcile(identity_id)
        self.assertEqual("canonical-1", canonical_id)
        self.assertEqual(canonical_id, self.reconcile(identity_id, "2"))
        self.assertEqual(1, len(self.store.links(self.binding_id())))

    def test_normalized_match_requires_review_and_create_new_is_idempotent(self) -> None:
        identity_id = self.publish(identity("recording-b"), "scan-b")
        self.connection.execute(
            "INSERT INTO canonical_tracks VALUES (?, ?, ?)",
            ("recording-a", "Invented Song", "2026-01-01T00:00:00+00:00"),
        )
        self.connection.execute(
            "INSERT INTO canonical_track_artists VALUES (?, ?, ?)",
            ("recording-a", 0, "Synthetic Artist"),
        )
        self.connection.commit()
        self.assertEqual("review_required", self.reconcile(identity_id))
        self.assertEqual("review_required", self.reconcile(identity_id, "again"))
        candidates = self.store.candidates(self.binding_id())
        self.assertEqual(2, len(candidates))
        self.assertEqual(1, sum(item.canonical_track_id is None for item in candidates))
        self.assertEqual(0, len(self.store.links(self.binding_id())))

    def test_rejection_is_idempotent_and_changed_evidence_can_repropose(self) -> None:
        identity_id = self.publish(identity("legacy"), "scan-legacy")
        self.reconcile(identity_id)
        self.store.unlink_identity(self.binding_id(), identity_id, "unlink-event")
        self.assertEqual("review_required", self.reconcile(identity_id, "review"))
        for candidate in self.store.candidates(self.binding_id()):
            if candidate.state in {
                MatchCandidateState.CANDIDATE,
                MatchCandidateState.AMBIGUOUS,
                MatchCandidateState.USER_CONFIRMATION_REQUIRED,
            }:
                self.store.reject_candidate(self.binding_id(), candidate.candidate_id)
        self.assertEqual("user_resolved", self.reconcile(identity_id, "repeat"))
        count = len(self.store.candidates(self.binding_id()))

        self.publish(identity("legacy", title="Invented Song Remaster", version="remaster"), "scan-changed")
        self.assertEqual("review_required", self.reconcile(identity_id, "changed"))
        self.assertGreater(len(self.store.candidates(self.binding_id())), count)

    def test_confirmation_supersedes_peers_and_unlink_preserves_history(self) -> None:
        identity_id = self.publish(identity("confirm"), "scan-confirm")
        self.connection.execute(
            "INSERT INTO canonical_tracks VALUES ('existing', 'Invented Song', '2026-01-01T00:00:00+00:00')"
        )
        self.connection.execute(
            "INSERT INTO canonical_track_artists VALUES ('existing', 0, 'Synthetic Artist')"
        )
        self.connection.commit()
        self.reconcile(identity_id)
        candidates = self.store.candidates(self.binding_id())
        existing = next(item for item in candidates if item.canonical_track_id == "existing")
        self.store.confirm_candidate(
            self.binding_id(), existing.candidate_id,
            canonical_id="existing", create_new=False,
            link_id="confirmed-link", event_id="confirmed-event",
        )
        states = {item.candidate_id: item.state for item in self.store.candidates(self.binding_id())}
        self.assertEqual(MatchCandidateState.CONFIRMED, states[existing.candidate_id])
        self.assertIn(MatchCandidateState.SUPERSEDED, states.values())
        self.store.unlink_identity(self.binding_id(), identity_id, "unlink-history")
        self.assertEqual(0, len(self.store.links(self.binding_id())))
        self.assertEqual(
            ("link", "unlink"),
            tuple(row[0] for row in self.connection.execute(
                "SELECT action FROM canonical_identity_link_events "
                "WHERE account_binding_id = ? AND identity_id = ? ORDER BY event_at, event_id",
                (self.binding_id(), identity_id),
            )),
        )

    def test_confirmation_failure_rolls_back_candidate_and_link(self) -> None:
        identity_id = self.publish(identity("rollback-confirm"), "scan-rollback-confirm")
        self.reconcile(identity_id)
        self.store.unlink_identity(self.binding_id(), identity_id, "unlink-before-review")
        self.reconcile(identity_id, "review")
        candidate = self.store.candidates(self.binding_id())[-1]
        self.connection.execute(
            "INSERT INTO canonical_tracks VALUES ('duplicate', 'Other', '2026-01-01T00:00:00+00:00')"
        )
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.confirm_candidate(
                self.binding_id(), candidate.candidate_id,
                canonical_id="duplicate", create_new=True,
                link_id="should-not-exist", event_id="should-not-exist",
            )
        self.assertEqual(MatchCandidateState.USER_CONFIRMATION_REQUIRED, self.store.candidate_review(self.binding_id(), candidate.candidate_id).candidate.state)
        self.assertEqual(0, len(self.store.links(self.binding_id())))

    def test_confirm_new_creates_distinct_canonical_and_supersedes_peers(self) -> None:
        identity_id = self.publish(identity("confirm-new"), "scan-confirm-new")
        self.reconcile(identity_id)
        self.store.unlink_identity(self.binding_id(), identity_id, "unlink-confirm-new")
        self.reconcile(identity_id, "new-review")
        candidate = next(
            item for item in self.store.candidates(self.binding_id())
            if item.action.value == "create_new"
            and item.state is MatchCandidateState.USER_CONFIRMATION_REQUIRED
        )
        service = Phase3ReviewService(
            store=self.store,
            binding_id=self.binding_id(),
            id_factory=lambda prefix: f"review-{prefix}",
        )
        canonical_id = service.confirm_new(candidate.candidate_id)
        self.assertEqual(canonical_id, self.store.links(self.binding_id())[0].canonical.canonical_track_id)
        self.assertEqual(
            MatchCandidateState.CONFIRMED,
            self.store.candidate_review(self.binding_id(), candidate.candidate_id).candidate.state,
        )

    def test_second_account_does_not_inherit_first_account_link(self) -> None:
        identity_id = self.publish(identity("shared"), "scan-account-1")
        first_canonical = self.reconcile(identity_id)
        first_binding = self.binding_id()
        self.store.disable_source(self.source_id, first_binding)
        self.store.unbind_account()

        self.reader.account = PlatformAccountIdentity("account-2", "user-2")
        self.coordinator.bind_account()
        second_source = self.coordinator.register_liked()
        self.publish_direct(second_source, identity("shared"), "scan-account-2")
        result = self.reconcile(identity_id, "account-2")
        self.assertEqual("review_required", result)
        self.assertEqual(0, len(self.store.links(self.binding_id())))
        self.assertEqual(
            first_canonical,
            self.connection.execute(
                "SELECT canonical_track_id FROM account_canonical_identity_links "
                "WHERE account_binding_id = ? AND identity_id = ?",
                (first_binding, identity_id),
            ).fetchone()[0],
        )
        self.assertTrue(any(
            item.canonical_track_id == first_canonical
            and item.state is MatchCandidateState.USER_CONFIRMATION_REQUIRED
            for item in self.store.candidates(self.binding_id())
        ))

    def test_review_candidate_lookup_rejects_another_accounts_candidate(self) -> None:
        identity_id = self.publish(identity("scoped-review"), "scan-scoped-review")
        self.connection.execute(
            "INSERT INTO canonical_tracks VALUES (?, ?, ?)",
            ("scoped-existing", "Invented Song", "2026-01-01T00:00:00+00:00"),
        )
        self.connection.execute(
            "INSERT INTO canonical_track_artists VALUES (?, ?, ?)",
            ("scoped-existing", 0, "Synthetic Artist"),
        )
        self.connection.commit()
        self.assertEqual("review_required", self.reconcile(identity_id, "scoped"))
        first_candidate = self.store.candidates(self.binding_id())[0]
        first_binding = self.binding_id()
        self.store.disable_source(self.source_id, first_binding)
        self.store.unbind_account()

        self.reader.account = PlatformAccountIdentity("account-2", "user-2")
        self.coordinator.bind_account()
        second_account_review = Phase3ReviewService(
            store=self.store,
            binding_id=self.binding_id(),
        )
        with self.assertRaisesRegex(Phase3StateError, "account-scoped"):
            second_account_review.candidate(first_candidate.candidate_id)

    def test_matching_failure_retries_without_reenumerating_unchanged_playlist(self) -> None:
        values = (
            identity("recover-1", title="Synthetic Recovery One"),
            identity("recover-2", title="Synthetic Recovery Two"),
        )
        self.reader.playlist_summaries = deque([summary(), summary()])
        self.reader.playlist_pages = {None: page(0, values, total=2)}
        original = self.store.reconcile_published_identity
        calls = 0

        def fail_second(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic matching failure")
            return original(*args, **kwargs)

        self.store.reconcile_published_identity = fail_second  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "matching"):
            self.coordinator.scan(self.source_id)
        publication = self.store.current_publication(self.source_id, self.binding_id())
        self.assertIsNotNone(publication)
        self.assertEqual([None], self.reader.playlist_item_cursors)

        self.store.reconcile_published_identity = original  # type: ignore[method-assign]
        result = self.coordinator.scan(self.source_id)
        self.assertEqual(publication["scan_id"], result)  # type: ignore[index]
        self.assertEqual([None], self.reader.playlist_item_cursors)
        self.assertEqual(2, len(self.store.links(self.binding_id())))

    def test_local_review_service_projects_concise_account_state(self) -> None:
        identity_id = self.publish(identity("review", version="acoustic"), "scan-review")
        self.reconcile(identity_id)
        service = Phase3ReviewService(store=self.store, binding_id=self.binding_id())
        self.assertEqual(1, service.status().linked_identity_count)
        shown = service.identity(identity_id)
        self.assertEqual("acoustic", shown.identity.version_label)
        self.assertEqual(1, len(service.links()))


class PortInvariantTests(unittest.TestCase):
    def test_track_page_requires_exact_ordered_coverage(self) -> None:
        value = identity("coverage")
        with self.assertRaisesRegex(ValueError, "consecutive"):
            TrackIdentityPage(
                items=(value,), next_cursor=None, snapshot_id=None,
                entries=(PlatformTrackReadEntry(1, identity=value),),
                offset=0, limit=1, total=1,
            )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            TrackIdentityPage(
                items=(value,), next_cursor=None, snapshot_id=None,
                entries=(), offset=0, limit=1, total=1,
            )


if __name__ == "__main__":
    unittest.main()
