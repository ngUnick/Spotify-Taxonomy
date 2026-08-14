"""Platform adapter contracts kept separate from classification and research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from spotify_playlist_taxonomy.domain.models import PlatformTrackIdentity


@dataclass(frozen=True, slots=True)
class PlaylistPage:
    items: tuple[PlatformTrackIdentity, ...]
    next_cursor: str | None
    snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class PlannedMutation:
    operation_id: str
    target_playlist_id: str
    item_ids: tuple[str, ...]


class MusicPlatformReader(Protocol):
    def read_playlist_items(
        self, playlist_id: str, cursor: str | None = None
    ) -> PlaylistPage: ...


class GuardedMusicPlatformWriter(Protocol):
    """Only an approved guarded executor may implement and receive this port."""

    def create_managed_playlist(self, operation: PlannedMutation) -> str: ...

    def add_items(self, operation: PlannedMutation) -> str | None: ...
