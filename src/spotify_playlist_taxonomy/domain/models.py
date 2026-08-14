"""Small, platform-neutral domain contracts established during Phase 0."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MAX_JUSTIFICATION_LENGTH: Final = 280


class IdentityProvider(StrEnum):
    """Origin of a track identity or platform mapping, not research evidence."""

    SPOTIFY = "spotify"
    MUSICBRAINZ = "musicbrainz"
    USER_IMPORT = "user_import"
    USER_ENTRY = "user_entry"
    OTHER_PLATFORM = "other_platform"


class ResearchSource(StrEnum):
    """External or user source from which normalized research evidence came."""

    LASTFM = "lastfm"
    MUSICBRAINZ = "musicbrainz"
    WIKIPEDIA = "wikipedia"
    OFFICIAL_ARTIST = "official_artist"
    OFFICIAL_LABEL = "official_label"
    PUBLICATION = "publication"
    GENERAL_WEB = "general_web"
    USER = "user"


class EvidenceKind(StrEnum):
    SOURCED_FACT = "sourced_fact"
    COMMUNITY_TAG = "community_tag"
    EDITORIAL_DESCRIPTION = "editorial_description"
    USER_ASSERTION = "user_assertion"


class CharacteristicOrigin(StrEnum):
    DETERMINISTIC_RULE = "deterministic_rule"
    CODEX_INTERPRETATION = "codex_interpretation"
    USER_EXPLICIT = "user_explicit"


class ProposalOrigin(StrEnum):
    CODEX_CLASSIFIER = "codex_classifier"
    GENRE_MAPPING_RULE = "genre_mapping_rule"
    CHARACTERISTIC_RULE = "characteristic_rule"
    USER_IMPORTED_PROPOSAL = "user_imported_proposal"


class DecisionOrigin(StrEnum):
    USER_APPROVAL = "user_approval"
    USER_OVERRIDE = "user_override"
    USER_EXPLICIT = "user_explicit"


class OperationalOrigin(StrEnum):
    SYSTEM_OPERATIONAL = "system_operational"


class CalibrationPolarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class AssignmentDecision(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    ABSTAIN = "abstain"


class ReviewStatus(StrEnum):
    PROPOSED = "proposed"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class MatchState(StrEnum):
    UNMATCHED = "unmatched"
    CANDIDATE = "candidate"
    AMBIGUOUS = "ambiguous"
    USER_CONFIRMATION_REQUIRED = "user_confirmation_required"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class PlaylistRole(StrEnum):
    SOURCE = "source"
    INBOX = "inbox"
    MANAGED = "managed"


class MutationKind(StrEnum):
    CREATE_PLAYLIST = "create_playlist"
    ADD_ITEMS = "add_items"
    REMOVE_ITEMS = "remove_items"
    REPLACE_ITEMS = "replace_items"
    CLEAR_PLAYLIST = "clear_playlist"
    REORDER_ITEMS = "reorder_items"
    CHANGE_DETAILS = "change_details"
    CHANGE_VISIBILITY = "change_visibility"
    DELETE_PLAYLIST = "delete_playlist"


def _validate_confidence(value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0 and 1")


def _validate_summary(value: str) -> None:
    if not value.strip():
        raise ValueError("justification_summary must not be blank")
    if len(value) > MAX_JUSTIFICATION_LENGTH:
        raise ValueError(
            f"justification_summary must be <= {MAX_JUSTIFICATION_LENGTH} characters"
        )


def _validate_nonblank(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _validate_artists(artists: tuple[str, ...]) -> None:
    if not artists or any(not artist.strip() for artist in artists):
        raise ValueError("at least one nonblank artist is required")


@dataclass(frozen=True, slots=True)
class CanonicalTrack:
    canonical_track_id: str
    display_title: str
    display_artists: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_nonblank("canonical_track_id", self.canonical_track_id)
        _validate_nonblank("display_title", self.display_title)
        _validate_artists(self.display_artists)


@dataclass(frozen=True, slots=True)
class PlatformTrackIdentity:
    identity_id: str
    provider: IdentityProvider
    title: str
    artists: tuple[str, ...]
    provider_track_id: str | None = None
    version_label: str | None = None

    def __post_init__(self) -> None:
        _validate_nonblank("identity_id", self.identity_id)
        _validate_nonblank("title", self.title)
        _validate_artists(self.artists)
        if self.provider_track_id is not None:
            _validate_nonblank("provider_track_id", self.provider_track_id)
        if self.version_label is not None:
            _validate_nonblank("version_label", self.version_label)


@dataclass(frozen=True, slots=True)
class CanonicalTrackIdentityLink:
    link_id: str
    canonical_track_id: str
    identity_id: str

    def __post_init__(self) -> None:
        _validate_nonblank("link_id", self.link_id)
        _validate_nonblank("canonical_track_id", self.canonical_track_id)
        _validate_nonblank("identity_id", self.identity_id)


@dataclass(frozen=True, slots=True)
class TrackMatchAssessment:
    left_identity_id: str
    right_identity_id: str
    state: MatchState
    confidence: float
    basis: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        _validate_nonblank("left_identity_id", self.left_identity_id)
        _validate_nonblank("right_identity_id", self.right_identity_id)
        if self.left_identity_id == self.right_identity_id:
            raise ValueError("a match assessment requires two distinct identity records")
        if not self.basis or any(not item.strip() for item in self.basis):
            raise ValueError("match basis must contain nonblank values")


@dataclass(frozen=True, slots=True)
class PlatformPlaylistRegistration:
    registration_id: str
    provider: IdentityProvider
    provider_playlist_id: str
    role: PlaylistRole
    logical_playlist_id: str | None = None
    owner_account_id: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        _validate_nonblank("registration_id", self.registration_id)
        _validate_nonblank("provider_playlist_id", self.provider_playlist_id)
        for name, value in (
            ("logical_playlist_id", self.logical_playlist_id),
            ("owner_account_id", self.owner_account_id),
            ("display_name", self.display_name),
        ):
            if value is not None:
                _validate_nonblank(name, value)


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    evidence_id: str
    canonical_track_id: str
    source: ResearchSource
    kind: EvidenceKind
    normalized_value: str
    source_reference: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        _validate_nonblank("evidence_id", self.evidence_id)
        _validate_nonblank("canonical_track_id", self.canonical_track_id)
        _validate_nonblank("normalized_value", self.normalized_value)
        if self.source_reference is not None:
            _validate_nonblank("source_reference", self.source_reference)
        if self.confidence is not None:
            _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class DerivedCharacteristic:
    characteristic_id: str
    canonical_track_id: str
    name: str
    value: str
    origin: CharacteristicOrigin
    evidence_ids: tuple[str, ...]
    confidence: float
    derivation_version: str
    justification_summary: str

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        _validate_summary(self.justification_summary)
        for name, value in (
            ("characteristic_id", self.characteristic_id),
            ("canonical_track_id", self.canonical_track_id),
            ("name", self.name),
            ("value", self.value),
            ("derivation_version", self.derivation_version),
        ):
            _validate_nonblank(name, value)
        if self.origin is not CharacteristicOrigin.USER_EXPLICIT and not self.evidence_ids:
            raise ValueError("generated characteristics must reference evidence")


@dataclass(frozen=True, slots=True)
class ClassificationProposal:
    proposal_id: str
    canonical_track_id: str
    logical_playlist_id: str
    decision: AssignmentDecision
    confidence: float
    origin: ProposalOrigin
    evidence_ids: tuple[str, ...]
    characteristic_ids: tuple[str, ...]
    justification_summary: str
    taxonomy_version: str
    specification_version: str
    review_status: ReviewStatus

    def __post_init__(self) -> None:
        _validate_confidence(self.confidence)
        _validate_summary(self.justification_summary)
        for name, value in (
            ("proposal_id", self.proposal_id),
            ("canonical_track_id", self.canonical_track_id),
            ("logical_playlist_id", self.logical_playlist_id),
            ("taxonomy_version", self.taxonomy_version),
            ("specification_version", self.specification_version),
        ):
            _validate_nonblank(name, value)
        if not self.logical_playlist_id.startswith(("genre.", "vibe.", "activity.")):
            raise ValueError("logical_playlist_id must use a known taxonomy dimension")
        if not (self.evidence_ids or self.characteristic_ids):
            raise ValueError("a proposal must reference evidence or characteristics")


@dataclass(frozen=True, slots=True)
class UserDecision:
    decision_id: str
    canonical_track_id: str
    logical_playlist_id: str
    decision: AssignmentDecision
    origin: DecisionOrigin
    sequence: int

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("canonical_track_id", self.canonical_track_id),
            ("logical_playlist_id", self.logical_playlist_id),
        ):
            _validate_nonblank(name, value)
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class CalibrationExample:
    calibration_example_id: str
    user_decision_id: str
    logical_playlist_id: str
    polarity: CalibrationPolarity
    selected: bool = True

    def __post_init__(self) -> None:
        _validate_nonblank("calibration_example_id", self.calibration_example_id)
        _validate_nonblank("user_decision_id", self.user_decision_id)
        _validate_nonblank("logical_playlist_id", self.logical_playlist_id)


@dataclass(frozen=True, slots=True)
class EffectiveAssignment:
    decision: AssignmentDecision
    origin: DecisionOrigin | ProposalOrigin
    needs_review: bool
    source_id: str


@dataclass(frozen=True, slots=True)
class MutationContext:
    mutation: MutationKind
    playlist_role: PlaylistRole | None
    stable_playlist_id_registered: bool
    live_owner_validated: bool
    dry_run_approved: bool
    change_set_approved: bool
    target_exists: bool = True


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reasons: tuple[str, ...]
