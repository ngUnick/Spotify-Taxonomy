"""Minimal, read-only Spotify Web API adapter."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from typing import Callable, Protocol

import httpx

from spotify_playlist_taxonomy.domain.models import IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.ports.music_platform import (
    PlatformAccountIdentity,
    PlatformPlaylistSummary,
    PlatformReadIssue,
    PlatformReadIssueKind,
    PlatformTrackReadEntry,
    PlaylistEntrySchemaDiagnostics,
    PlaylistSummaryPage,
    TrackIdentityPage,
)

from .errors import SpotifyAdapterError, SpotifyErrorKind

API_ORIGIN = "https://api.spotify.com"
API_PREFIX = "/v1"
PAGE_SIZE = 50
MAX_SANE_RETRY_AFTER_SECONDS = 3600.0
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9]+$")
_ALLOWED_PATHS = (
    re.compile(r"^/me$"),
    re.compile(r"^/me/playlists$"),
    re.compile(r"^/me/tracks$"),
    re.compile(r"^/playlists/[A-Za-z0-9]+$"),
    re.compile(r"^/playlists/[A-Za-z0-9]+/items$"),
)


class TokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


@dataclass(frozen=True, slots=True)
class _ParsedTrack:
    identity: PlatformTrackIdentity | None
    issue: PlatformReadIssue | None


class SpotifyReadOnlyAdapter:
    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        http_client: httpx.Client,
        max_read_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not 1 <= max_read_attempts <= 10:
            raise ValueError("max_read_attempts must be between 1 and 10")
        self._tokens = token_provider
        self._http = http_client
        self._max_attempts = max_read_attempts
        self._sleep = sleeper
        self._now = now

    def read_current_account(self) -> PlatformAccountIdentity:
        payload = self._get("/me")
        return PlatformAccountIdentity(
            account_id=_required_string(payload, "account_id"),
            spotify_user_id=_required_string(payload, "id"),
        )

    def read_current_user_playlists(self, cursor: str | None = None) -> PlaylistSummaryPage:
        offset = _decode_cursor(cursor)
        payload = self._get("/me/playlists", {"limit": PAGE_SIZE, "offset": offset})
        raw_items = _required_list(payload, "items")
        items = tuple(_parse_playlist_summary(item) for item in raw_items)
        return PlaylistSummaryPage(items, _next_cursor(payload, offset, len(raw_items)))

    def read_playlist(self, playlist_id: str) -> PlatformPlaylistSummary:
        _validate_playlist_id(playlist_id)
        payload = self._get(
            f"/playlists/{playlist_id}",
            {"fields": "id,name,description,owner(id),collaborative,public,snapshot_id,items(total)"},
        )
        return _parse_playlist_summary(payload)

    def read_playlist_items(
        self, playlist_id: str, cursor: str | None = None
    ) -> TrackIdentityPage:
        _validate_playlist_id(playlist_id)
        offset = _decode_cursor(cursor)
        payload = self._get(
            f"/playlists/{playlist_id}/items",
            {
                "limit": PAGE_SIZE,
                "offset": offset,
                "fields": (
                    "items(is_local,item(id,name,type,is_local,artists(name))),"
                    "next,offset,limit,total"
                ),
            },
        )
        raw_items = _required_list(payload, "items")
        identities: list[PlatformTrackIdentity] = []
        issues: list[PlatformReadIssue] = []
        ordered: list[PlatformTrackReadEntry] = []
        canonical_count = 0
        for index, entry in enumerate(raw_items):
            if not isinstance(entry, dict):
                issue = PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED)
                issues.append(issue)
                ordered.append(PlatformTrackReadEntry(index, issue=issue))
                continue
            if "item" in entry:
                canonical_count += 1
                parsed = _parse_track(entry["item"], index, wrapper_local=entry.get("is_local"))
            else:
                parsed = _ParsedTrack(
                    None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED)
                )
            if parsed.identity is not None:
                identities.append(parsed.identity)
                ordered.append(PlatformTrackReadEntry(index, identity=parsed.identity))
            if parsed.issue is not None:
                issues.append(parsed.issue)
                ordered.append(PlatformTrackReadEntry(index, issue=parsed.issue))
        limit, total = _page_numbers(payload, len(raw_items))
        return TrackIdentityPage(
            items=tuple(identities),
            next_cursor=_next_cursor(payload, offset, len(raw_items)),
            snapshot_id=None,
            issues=tuple(issues),
            schema_diagnostics=PlaylistEntrySchemaDiagnostics(canonical_count),
            entries=tuple(ordered), offset=offset, limit=limit, total=total,
        )

    def read_saved_tracks(self, cursor: str | None = None) -> TrackIdentityPage:
        offset = _decode_cursor(cursor)
        payload = self._get(
            "/me/tracks",
            {
                "limit": PAGE_SIZE,
                "offset": offset,
                "fields": "items(track(id,name,type,is_local,artists(name))),next,offset,limit,total",
            },
        )
        raw_items = _required_list(payload, "items")
        identities: list[PlatformTrackIdentity] = []
        issues: list[PlatformReadIssue] = []
        ordered: list[PlatformTrackReadEntry] = []
        for index, entry in enumerate(raw_items):
            if not isinstance(entry, dict) or "track" not in entry:
                issue = PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED)
                issues.append(issue)
                ordered.append(PlatformTrackReadEntry(index, issue=issue))
                continue
            parsed = _parse_track(entry["track"], index, wrapper_local=None)
            if parsed.identity is not None:
                identities.append(parsed.identity)
                ordered.append(PlatformTrackReadEntry(index, identity=parsed.identity))
            if parsed.issue is not None:
                issues.append(parsed.issue)
                ordered.append(PlatformTrackReadEntry(index, issue=parsed.issue))
        limit, total = _page_numbers(payload, len(raw_items))
        return TrackIdentityPage(
            items=tuple(identities),
            next_cursor=_next_cursor(payload, offset, len(raw_items)),
            snapshot_id=None,
            issues=tuple(issues),
            entries=tuple(ordered), offset=offset, limit=limit, total=total,
        )

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        if not any(pattern.fullmatch(path) for pattern in _ALLOWED_PATHS):
            raise SpotifyAdapterError(
                SpotifyErrorKind.CONFIGURATION,
                "Spotify endpoint is not allowed by the read-only adapter",
            )
        url = f"{API_ORIGIN}{API_PREFIX}{path}"
        attempt = 0
        refreshed_after_401 = False
        while True:
            attempt += 1
            try:
                response = self._http.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._tokens.access_token()}"
                    },
                )
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < self._max_attempts:
                    self._sleep(_fallback_delay(attempt))
                    continue
                raise SpotifyAdapterError(
                    SpotifyErrorKind.NETWORK,
                    "Spotify read failed after transient network errors",
                ) from None

            if response.status_code == 401:
                if not refreshed_after_401:
                    self._tokens.access_token(force_refresh=True)
                    refreshed_after_401 = True
                    attempt -= 1
                    continue
                raise SpotifyAdapterError(
                    SpotifyErrorKind.NOT_AUTHORIZED,
                    "Spotify authorization was rejected after token refresh",
                )
            if response.status_code == 429:
                if _is_quota_exhausted(response):
                    raise SpotifyAdapterError(
                        SpotifyErrorKind.QUOTA_EXHAUSTED,
                        "Spotify API quota is exhausted; read processing stopped",
                    )
                if attempt < self._max_attempts:
                    delay = _parse_retry_after(response.headers.get("Retry-After"), self._now())
                    self._sleep(delay if delay is not None else _fallback_delay(attempt))
                    continue
                raise SpotifyAdapterError(
                    SpotifyErrorKind.RATE_LIMITED,
                    "Spotify rate limit remained active after bounded retries",
                )
            if response.status_code in {502, 503, 504} and attempt < self._max_attempts:
                self._sleep(_fallback_delay(attempt))
                continue
            if response.status_code == 403:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.FORBIDDEN,
                    "Spotify denied access to the requested read resource",
                )
            if response.status_code == 404:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.NOT_FOUND,
                    "Spotify read resource was not found",
                )
            if response.status_code >= 400:
                raise SpotifyAdapterError(
                    SpotifyErrorKind.NETWORK,
                    "Spotify read request failed",
                )
            return _json_mapping(response)


def _parse_playlist_summary(value: object) -> PlatformPlaylistSummary:
    if not isinstance(value, dict):
        raise _invalid_response()
    owner = value.get("owner")
    if not isinstance(owner, dict):
        raise _invalid_response()
    public = value.get("public")
    if public is not None and not isinstance(public, bool):
        raise _invalid_response()
    collaborative = value.get("collaborative")
    if not isinstance(collaborative, bool):
        raise _invalid_response()
    snapshot = value.get("snapshot_id")
    if snapshot is not None and (not isinstance(snapshot, str) or not snapshot):
        raise _invalid_response()
    description = value.get("description")
    if description is not None and not isinstance(description, str):
        raise _invalid_response()
    item_count: int | None = None
    if "items" in value and value["items"] is not None:
        item_summary = value["items"]
        if not isinstance(item_summary, dict):
            raise _invalid_response()
        total = item_summary.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise _invalid_response()
        item_count = total
    return PlatformPlaylistSummary(
        playlist_id=_required_string(value, "id"),
        name=_required_string(value, "name"),
        owner_user_id=_required_string(owner, "id"),
        collaborative=collaborative,
        public=public,
        item_count=item_count,
        snapshot_id=snapshot,
        description=description,
    )


def _parse_track(value: object, index: int, *, wrapper_local: object) -> _ParsedTrack:
    if value is None:
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.NULL))
    if not isinstance(value, dict):
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED))
    if wrapper_local is not None and not isinstance(wrapper_local, bool):
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED))
    object_local = value.get("is_local")
    if object_local is not None and not isinstance(object_local, bool):
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED))
    if wrapper_local is True or object_local is True:
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.LOCAL))
    item_type = value.get("type")
    if item_type != "track":
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.UNSUPPORTED))
    track_id = value.get("id")
    if track_id is None:
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.UNAVAILABLE))
    title = value.get("name")
    artists = value.get("artists")
    if (
        not isinstance(track_id, str)
        or not track_id
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(artists, list)
        or not artists
    ):
        return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED))
    artist_names: list[str] = []
    for artist in artists:
        if not isinstance(artist, dict):
            return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED))
        name = artist.get("name")
        if not isinstance(name, str) or not name.strip():
            return _ParsedTrack(None, PlatformReadIssue(index, PlatformReadIssueKind.MALFORMED))
        artist_names.append(name)
    return _ParsedTrack(
        PlatformTrackIdentity(
            identity_id=f"spotify:{track_id}",
            provider=IdentityProvider.SPOTIFY,
            provider_track_id=track_id,
            title=title,
            artists=tuple(artist_names),
        ),
        None,
    )


def _next_cursor(payload: dict[str, object], offset: int, item_count: int) -> str | None:
    next_value = payload.get("next")
    if next_value is None:
        return None
    if not isinstance(next_value, str) or not next_value:
        raise _invalid_response()
    if item_count == 0:
        raise _invalid_response()
    return _encode_cursor(offset + item_count)


def _page_numbers(payload: dict[str, object], item_count: int) -> tuple[int | None, int | None]:
    limit = payload.get("limit")
    total = payload.get("total")
    if limit is None:
        limit_value = item_count
    elif isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
        limit_value = limit
    else:
        raise _invalid_response()
    if total is None:
        total_value = None
    elif isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        total_value = total
    else:
        raise _invalid_response()
    return limit_value, total_value


def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": offset}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("ascii"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"offset"}
            or not isinstance(payload["offset"], int)
            or isinstance(payload["offset"], bool)
            or payload["offset"] < 0
        ):
            raise ValueError("invalid cursor")
        return payload["offset"]
    except Exception:
        raise SpotifyAdapterError(
            SpotifyErrorKind.INVALID_CURSOR,
            "Spotify pagination cursor is invalid",
        ) from None


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        seconds = float(int(text))
    else:
        try:
            target = parsedate_to_datetime(text)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_SANE_RETRY_AFTER_SECONDS:
        return None
    return seconds


def _fallback_delay(attempt: int) -> float:
    return float(min(attempt, 2))


def _is_quota_exhausted(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    reason = payload.get("reason")
    error = payload.get("error")
    if isinstance(error, dict):
        reason = error.get("reason", reason)
    return reason == "QUOTA_EXCEEDED"


def _json_mapping(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise _invalid_response() from None
    if not isinstance(payload, dict):
        raise _invalid_response()
    return payload


def _required_string(value: dict[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise _invalid_response()
    return item


def _required_list(value: dict[str, object], field: str) -> list[object]:
    item = value.get(field)
    if not isinstance(item, list):
        raise _invalid_response()
    return item


def _validate_playlist_id(playlist_id: str) -> None:
    if not _PLAYLIST_ID.fullmatch(playlist_id):
        raise SpotifyAdapterError(
            SpotifyErrorKind.CONFIGURATION,
            "Spotify playlist ID is invalid",
        )


def _invalid_response() -> SpotifyAdapterError:
    return SpotifyAdapterError(
        SpotifyErrorKind.INVALID_RESPONSE,
        "Spotify returned an invalid read response",
    )
