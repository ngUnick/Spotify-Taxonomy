"""Read-only Phase 3 ingestion and matching coordination."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

from spotify_playlist_taxonomy.domain.ingestion import (
    AccessibilityState,
    InboxKind,
    OwnershipState,
    ScanStatus,
    SourceKind,
    StagedSourceEntry,
)
from spotify_playlist_taxonomy.domain.models import PlaylistRole
from spotify_playlist_taxonomy.persistence.phase3 import Phase3StateError, Phase3Store
from spotify_playlist_taxonomy.ports.music_platform import (
    MusicPlatformReader,
    PlatformAccountIdentity,
    PlatformPlaylistSummary,
    PlatformReaderError,
    TrackIdentityPage,
)

DIGEST_VERSION = "source-occurrences.v1"
MATCHER_VERSION = "canonical-match.v1"


class Phase3Coordinator:
    def __init__(
        self,
        *,
        reader: MusicPlatformReader,
        store: Phase3Store,
        id_factory: Callable[[str], str] = lambda prefix: f"{prefix}.{uuid.uuid4().hex}",
    ) -> None:
        self.reader = reader
        self.store = store
        self.id_factory = id_factory

    def bind_account(self) -> PlatformAccountIdentity:
        account = self.reader.read_current_account()
        self.store.bind_account(self.id_factory("account"), account.account_id, account.spotify_user_id)
        return account

    def verify_account(self) -> PlatformAccountIdentity:
        account = self.reader.read_current_account()
        active = self.store.active_account()
        if active is None:
            raise Phase3StateError("Spotify account must be explicitly bound before ingestion")
        self.store.bind_account(active.binding_id, account.account_id, account.spotify_user_id)
        return account

    def discover_playlists(self) -> tuple[tuple[PlatformPlaylistSummary, OwnershipState], ...]:
        account = self.verify_account()
        cursor = None
        discovered: list[tuple[PlatformPlaylistSummary, OwnershipState]] = []
        while True:
            page = self.reader.read_current_user_playlists(cursor)
            discovered.extend(
                (item, ownership_state(item, account.spotify_user_id)) for item in page.items
            )
            if page.next_cursor is None:
                return tuple(discovered)
            cursor = page.next_cursor

    def register_playlist(
        self,
        playlist_id: str,
        *,
        role: PlaylistRole,
        inbox_kind: InboxKind | None = None,
    ) -> str:
        account = self.verify_account()
        binding = self.store.active_account()
        assert binding is not None
        summary = self.reader.read_playlist(playlist_id)
        source_id = self.id_factory("source")
        self.store.register_playlist_source(
            source_id=source_id,
            registration_id=self.id_factory("playlist-registration"),
            binding_id=binding.binding_id,
            summary=summary,
            role=role,
            ownership=ownership_state(summary, account.spotify_user_id),
            inbox_kind=inbox_kind,
        )
        return source_id

    def register_liked(self) -> str:
        self.verify_account()
        binding = self.store.active_account()
        assert binding is not None
        source_id = self.id_factory("source")
        self.store.register_liked_source(source_id, binding.binding_id)
        return source_id

    def scan(self, source_id: str, *, resume: bool = False) -> str:
        self.verify_account()
        binding = self.store.active_account()
        assert binding is not None
        source = self.store.source_for_binding(source_id, binding.binding_id)
        if not source.active:
            raise Phase3StateError("disabled ingestion sources cannot be scanned")
        if source.kind is SourceKind.PLAYLIST:
            return self._scan_playlist(source_id, binding.binding_id, resume=resume)
        if resume:
            raise Phase3StateError("Saved Tracks scans cannot resume and must restart at offset 0")
        return self._scan_saved_tracks(source_id, binding.binding_id)

    def _scan_playlist(self, source_id: str, binding_id: str, *, resume: bool) -> str:
        source = self.store.source_for_binding(source_id, binding_id)
        assert source.provider_source_id is not None
        account = self.store.active_account()
        assert account is not None
        try:
            starting = self.reader.read_playlist(source.provider_source_id)
        except PlatformReaderError as error:
            self._record_access_failure(source_id, binding_id, error)
            raise
        self.store.observe_playlist(
            source_id,
            binding_id,
            starting,
            ownership_state(starting, account.current_user_id),
            AccessibilityState.UNKNOWN.value,
        )

        publication = self.store.current_publication(source_id, binding_id)
        if (
            publication is not None
            and starting.snapshot_id is not None
            and publication["ending_snapshot_id"] == starting.snapshot_id
        ):
            self._match_published_scan(binding_id, publication["scan_id"])
            return publication["scan_id"]

        resumable = self.store.resumable_playlist_scan(source_id, binding_id) if resume else None
        if resumable is not None:
            if starting.snapshot_id is None or resumable["starting_snapshot_id"] is None:
                self.store.mark_scan(
                    resumable["scan_id"], binding_id, ScanStatus.ABANDONED,
                    "snapshot_unavailable",
                )
                resumable = None
            elif resumable["starting_snapshot_id"] != starting.snapshot_id:
                self.store.mark_scan(
                    resumable["scan_id"], binding_id, ScanStatus.STALE, "snapshot_changed"
                )
                resumable = None
        if resumable is None:
            scan_id = self.id_factory("scan")
            self.store.start_scan(
                scan_id=scan_id,
                source_id=source_id,
                binding_id=binding_id,
                starting_snapshot_id=starting.snapshot_id,
                starting_total=starting.item_count,
                initial_page_signature=None,
                digest_version=DIGEST_VERSION,
            )
            cursor = None
            page_ordinal = 0
        else:
            scan_id = resumable["scan_id"]
            cursor = resumable["resume_cursor"]
            page_ordinal = resumable["page_count"]
            if resumable["status"] == ScanStatus.FAILED.value:
                self.store.mark_scan(scan_id, binding_id, ScanStatus.RUNNING)

        try:
            while True:
                page = self.reader.read_playlist_items(source.provider_source_id, cursor)
                self.store.stage_page(
                    scan_id=scan_id,
                    binding_id=binding_id,
                    entries=_staged_entries(page, page_ordinal),
                    next_cursor=page.next_cursor,
                    next_offset=_next_offset(page),
                )
                page_ordinal += 1
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
            ending = self.reader.read_playlist(source.provider_source_id)
            self.store.observe_playlist(
                source_id,
                binding_id,
                ending,
                ownership_state(ending, account.current_user_id),
                AccessibilityState.ACCESSIBLE.value,
            )
            if ending.snapshot_id != starting.snapshot_id:
                self.store.mark_scan(
                    scan_id, binding_id, ScanStatus.STALE, "snapshot_changed"
                )
                return scan_id
            self.store.publish_scan(scan_id, binding_id, ending.snapshot_id)
            self._match_published_scan(binding_id, scan_id)
            return scan_id
        except PlatformReaderError as error:
            self._record_access_failure(source_id, binding_id, error)
            self._fail_running_scan(scan_id, binding_id, "read_failed")
            raise
        except Exception:
            self._fail_running_scan(scan_id, binding_id, "read_failed")
            raise

    def _scan_saved_tracks(self, source_id: str, binding_id: str) -> str:
        first_page = self.reader.read_saved_tracks(None)
        signature = _initial_page_signature(first_page)
        scan_id = self.id_factory("scan")
        self.store.start_scan(
            scan_id=scan_id,
            source_id=source_id,
            binding_id=binding_id,
            starting_snapshot_id=None,
            starting_total=first_page.total,
            initial_page_signature=signature,
            digest_version=DIGEST_VERSION,
        )
        try:
            page = first_page
            page_ordinal = 0
            while True:
                self.store.stage_page(
                    scan_id=scan_id,
                    binding_id=binding_id,
                    entries=_staged_entries(page, page_ordinal),
                    next_cursor=page.next_cursor,
                    next_offset=_next_offset(page),
                )
                if page.next_cursor is None:
                    break
                page_ordinal += 1
                page = self.reader.read_saved_tracks(page.next_cursor)
            ending_first_page = self.reader.read_saved_tracks(None)
            if (
                ending_first_page.total != first_page.total
                or _initial_page_signature(ending_first_page) != signature
            ):
                self.store.mark_scan(
                    scan_id, binding_id, ScanStatus.STALE, "saved_tracks_changed"
                )
                return scan_id
            self.store.publish_scan(scan_id, binding_id, None)
            self._match_published_scan(binding_id, scan_id)
            return scan_id
        except Exception:
            self._fail_running_scan(scan_id, binding_id, "read_failed")
            raise

    def _match_published_scan(self, binding_id: str, scan_id: str) -> None:
        for identity_id in self.store.published_identity_ids(binding_id, scan_id):
            self.store.reconcile_published_identity(
                binding_id,
                identity_id,
                matcher_version=MATCHER_VERSION,
                candidate_id=self.id_factory("candidate"),
                canonical_id=self.id_factory("canonical"),
                link_id=self.id_factory("link"),
                event_id=self.id_factory("link-event"),
            )

    def _fail_running_scan(self, scan_id: str, binding_id: str, kind: str) -> None:
        if self.store.scan_row(scan_id, binding_id)["status"] == ScanStatus.RUNNING.value:
            self.store.mark_scan(scan_id, binding_id, ScanStatus.FAILED, kind)

    def _record_access_failure(
        self, source_id: str, binding_id: str, error: PlatformReaderError
    ) -> None:
        if error.access_state is not None:
            self.store.record_playlist_accessibility(
                source_id, binding_id, error.access_state.value
            )


def ownership_state(
    summary: PlatformPlaylistSummary, current_user_id: str | None
) -> OwnershipState:
    if not summary.owner_user_id or not current_user_id:
        return OwnershipState.UNKNOWN
    return (
        OwnershipState.OWNED
        if summary.owner_user_id == current_user_id
        else OwnershipState.NON_OWNED
    )


def _staged_entries(
    page: TrackIdentityPage, page_ordinal: int
) -> tuple[StagedSourceEntry, ...]:
    return tuple(
        StagedSourceEntry(
            source_position=page.offset + entry.item_index,
            page_ordinal=page_ordinal,
            identity=entry.identity,
            issue_kind=None if entry.issue is None else entry.issue.kind.value,
        )
        for entry in page.entries
    )


def _next_offset(page: TrackIdentityPage) -> int:
    return page.offset if not page.entries else page.offset + len(page.entries)


def _initial_page_signature(page: TrackIdentityPage) -> str:
    digest = hashlib.sha256()
    for entry in page.entries:
        if entry.identity is not None:
            value = f"spotify:{entry.identity.provider_track_id}"
        else:
            assert entry.issue is not None
            value = f"!{entry.issue.kind.value}"
        digest.update(f"{entry.item_index}:{value}\n".encode("utf-8"))
    return digest.hexdigest()
