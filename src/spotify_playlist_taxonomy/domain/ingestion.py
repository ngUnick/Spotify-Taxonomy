"""Phase 3 read-only ingestion and canonical-matching records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import CanonicalTrack, PlatformTrackIdentity, PlaylistRole


class OwnershipState(StrEnum):
    OWNED = "owned"
    NON_OWNED = "non_owned"
    UNKNOWN = "unknown"


class AccessibilityState(StrEnum):
    UNKNOWN = "unknown"
    ACCESSIBLE = "accessible"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"


class SourceKind(StrEnum):
    PLAYLIST = "playlist"
    SAVED_TRACKS = "saved_tracks"


class InboxKind(StrEnum):
    LIKED_SONGS = "liked_songs"
    SHAZAM = "shazam"


class ScanStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    ABANDONED = "abandoned"


class MatchCandidateAction(StrEnum):
    LINK_EXISTING = "link_existing"
    CREATE_NEW = "create_new"


class MatchCandidateState(StrEnum):
    CANDIDATE = "candidate"
    AMBIGUOUS = "ambiguous"
    USER_CONFIRMATION_REQUIRED = "user_confirmation_required"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class LinkEventAction(StrEnum):
    LINK = "link"
    UNLINK = "unlink"
    RELINK = "relink"
    CREATE_NEW = "create_new"


class LinkDecisionOrigin(StrEnum):
    SYSTEM_BOOTSTRAP = "system_bootstrap"
    USER_CONFIRMATION = "user_confirmation"
    USER_CREATE_NEW = "user_create_new"
    USER_UNLINK = "user_unlink"


@dataclass(frozen=True, slots=True)
class AccountBinding:
    binding_id: str
    account_id: str
    current_user_id: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class IngestionSource:
    source_id: str
    binding_id: str
    kind: SourceKind
    role: PlaylistRole
    provider_source_id: str | None
    inbox_kind: InboxKind | None
    active: bool = True


@dataclass(frozen=True, slots=True)
class StagedSourceEntry:
    source_position: int
    page_ordinal: int
    identity: PlatformTrackIdentity | None = None
    issue_kind: str | None = None

    def __post_init__(self) -> None:
        if self.source_position < 0 or self.page_ordinal < 0:
            raise ValueError("source positions and page ordinals must be non-negative")
        if (self.identity is None) == (self.issue_kind is None):
            raise ValueError("a staged entry requires exactly one identity or issue")


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    candidate_id: str
    account_binding_id: str
    identity_id: str
    canonical_track_id: str | None
    action: MatchCandidateAction
    state: MatchCandidateState
    confidence: float
    basis: tuple[str, ...]
    matcher_version: str


@dataclass(frozen=True, slots=True)
class CatalogueStatus:
    account_binding_id: str
    published_identity_count: int
    linked_identity_count: int
    reviewable_candidate_count: int


@dataclass(frozen=True, slots=True)
class IdentityReview:
    identity: PlatformTrackIdentity
    linked_canonical: CanonicalTrack | None
    candidates: tuple[MatchCandidate, ...]


@dataclass(frozen=True, slots=True)
class CandidateReview:
    candidate: MatchCandidate
    identity: PlatformTrackIdentity
    canonical: CanonicalTrack | None


@dataclass(frozen=True, slots=True)
class LinkReview:
    link_id: str
    identity: PlatformTrackIdentity
    canonical: CanonicalTrack
