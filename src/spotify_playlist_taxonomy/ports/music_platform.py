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


class PlatformAccessState(StrEnum):
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"


class PlatformReaderError(RuntimeError):
    """Sanitized reader failure with optional structured accessibility state."""

    def __init__(
        self, message: str, *, access_state: PlatformAccessState | None = None
    ) -> None:
        self.access_state = access_state
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PlatformReadIssue:
    item_index: int
    kind: PlatformReadIssueKind


@dataclass(frozen=True, slots=True)
class PlatformTrackReadEntry:
    item_index: int
    identity: PlatformTrackIdentity | None = None
    issue: PlatformReadIssue | None = None

    def __post_init__(self) -> None:
        if self.item_index < 0 or (self.identity is None) == (self.issue is None):
            raise ValueError("ordered platform entries require one identity or issue")
        if self.issue is not None and self.issue.item_index != self.item_index:
            raise ValueError("ordered issue index must match its entry index")


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
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PlaylistEntrySchemaDiagnostics:
    canonical_item_count: int = 0


@dataclass(frozen=True, slots=True)
class TrackIdentityPage:
    items: tuple[PlatformTrackIdentity, ...]
    next_cursor: str | None
    snapshot_id: str | None
    issues: tuple[PlatformReadIssue, ...] = ()
    schema_diagnostics: PlaylistEntrySchemaDiagnostics | None = None
    entries: tuple[PlatformTrackReadEntry, ...] = ()
    offset: int = 0
    limit: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("page offset must be non-negative")
        if self.limit is not None and self.limit < 0:
            raise ValueError("page limit must be non-negative")
        if self.total is not None and self.total < 0:
            raise ValueError("page total must be non-negative")
        if self.total is not None and self.offset > self.total and self.entries:
            raise ValueError("non-empty page offset cannot exceed total")
        if self.total is not None and self.offset + len(self.entries) > self.total:
            raise ValueError("page entries cannot extend beyond total")
        if self.limit is not None and len(self.entries) > self.limit:
            raise ValueError("ordered entries cannot exceed the page limit")
        if self.next_cursor is not None and not self.entries:
            raise ValueError("a continuing page must contain ordered entries")
        indices = tuple(entry.item_index for entry in self.entries)
        if indices != tuple(range(len(self.entries))):
            raise ValueError("ordered page entries must cover consecutive item indexes")
        entry_items = tuple(
            entry.identity for entry in self.entries if entry.identity is not None
        )
        entry_issues = tuple(
            entry.issue for entry in self.entries if entry.issue is not None
        )
        if entry_items != self.items or entry_issues != self.issues:
            raise ValueError("page items and issues must exactly match ordered entries")


@dataclass(frozen=True, slots=True)
class PlaylistSummaryPage:
    items: tuple[PlatformPlaylistSummary, ...]
    next_cursor: str | None


# Phase 0 name retained as a source-compatible alias.
PlaylistPage = TrackIdentityPage


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
