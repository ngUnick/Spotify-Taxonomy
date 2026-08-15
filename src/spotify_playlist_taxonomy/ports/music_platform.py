"""Platform adapter contracts kept separate from classification and research."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from spotify_playlist_taxonomy.domain.models import PlatformTrackIdentity


class PlatformReadIssueKind(StrEnum):
    UNAVAILABLE = "unavailable"
    LOCAL = "local"
    UNSUPPORTED = "unsupported"
    NULL = "null"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class PlatformReadIssue:
    item_index: int
    kind: PlatformReadIssueKind


@dataclass(frozen=True, slots=True)
class PlatformAccountIdentity:
    account_id: str
    spotify_user_id: str


@dataclass(frozen=True, slots=True)
class PlatformPlaylistSummary:
    playlist_id: str
    name: str
    owner_user_id: str
    collaborative: bool
    public: bool | None
    item_count: int | None
    snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class PlaylistEntrySchemaDiagnostics:
    canonical_item_count: int = 0
    legacy_track_count: int = 0


@dataclass(frozen=True, slots=True)
class TrackIdentityPage:
    items: tuple[PlatformTrackIdentity, ...]
    next_cursor: str | None
    snapshot_id: str | None
    issues: tuple[PlatformReadIssue, ...] = ()
    schema_diagnostics: PlaylistEntrySchemaDiagnostics | None = None


@dataclass(frozen=True, slots=True)
class PlaylistSummaryPage:
    items: tuple[PlatformPlaylistSummary, ...]
    next_cursor: str | None


# Phase 0 name retained as a source-compatible alias.
PlaylistPage = TrackIdentityPage


@dataclass(frozen=True, slots=True)
class PlannedMutation:
    operation_id: str
    target_playlist_id: str
    item_ids: tuple[str, ...]


class MusicPlatformReader(Protocol):
    def read_current_account(self) -> PlatformAccountIdentity: ...

    def read_current_user_playlists(
        self, cursor: str | None = None
    ) -> PlaylistSummaryPage: ...

    def read_playlist(self, playlist_id: str) -> PlatformPlaylistSummary: ...

    def read_playlist_items(
        self, playlist_id: str, cursor: str | None = None
    ) -> TrackIdentityPage: ...

    def read_saved_tracks(self, cursor: str | None = None) -> TrackIdentityPage: ...


class GuardedMusicPlatformWriter(Protocol):
    """Only an approved guarded executor may implement and receive this port."""

    def create_managed_playlist(self, operation: PlannedMutation) -> str: ...

    def add_items(self, operation: PlannedMutation) -> str | None: ...
