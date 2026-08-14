"""Pure policies that encode Phase 0 identity, decision, and write invariants."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from .models import (
    AssignmentDecision,
    AuthorizationDecision,
    ClassificationProposal,
    DecisionOrigin,
    EffectiveAssignment,
    MatchState,
    MutationContext,
    MutationKind,
    PlatformTrackIdentity,
    PlaylistRole,
    ProposalOrigin,
    TrackMatchAssessment,
    UserDecision,
)

_DECISION_PRECEDENCE = {
    DecisionOrigin.SYSTEM_OPERATIONAL: 0,
    DecisionOrigin.USER_APPROVAL: 2,
    DecisionOrigin.USER_OVERRIDE: 3,
    DecisionOrigin.USER_EXPLICIT: 3,
}
_ALLOWED_V1_MUTATIONS = {MutationKind.CREATE_PLAYLIST, MutationKind.ADD_ITEMS}


def normalize_identity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def normalized_identity_key(identity: PlatformTrackIdentity) -> tuple[str, tuple[str, ...]]:
    return (
        normalize_identity_text(identity.title),
        tuple(sorted(normalize_identity_text(artist) for artist in identity.artists)),
    )


def assess_track_match(
    left: PlatformTrackIdentity, right: PlatformTrackIdentity
) -> TrackMatchAssessment:
    """Assess, but never force, identity from title/artist normalization alone."""
    if left.identity_id == right.identity_id:
        raise ValueError("identity records must be distinct")

    if (
        left.provider is right.provider
        and left.provider_track_id
        and left.provider_track_id == right.provider_track_id
    ):
        return TrackMatchAssessment(
            left.identity_id,
            right.identity_id,
            MatchState.CONFIRMED,
            1.0,
            ("same_provider_stable_id",),
        )

    same_normalized_identity = normalized_identity_key(left) == normalized_identity_key(right)
    versions_conflict = bool(
        left.version_label
        and right.version_label
        and normalize_identity_text(left.version_label)
        != normalize_identity_text(right.version_label)
    )
    if same_normalized_identity and versions_conflict:
        return TrackMatchAssessment(
            left.identity_id,
            right.identity_id,
            MatchState.USER_CONFIRMATION_REQUIRED,
            0.25,
            ("normalized_title_artist_match", "conflicting_version_labels"),
        )
    if same_normalized_identity:
        return TrackMatchAssessment(
            left.identity_id,
            right.identity_id,
            MatchState.AMBIGUOUS,
            0.5,
            ("normalized_title_artist_match_only",),
        )
    return TrackMatchAssessment(
        left.identity_id,
        right.identity_id,
        MatchState.UNMATCHED,
        0.0,
        ("normalized_identity_differs",),
    )


def resolve_effective_assignment(
    proposals: Iterable[ClassificationProposal],
    decisions: Iterable[UserDecision],
) -> EffectiveAssignment | None:
    proposal_list = list(proposals)
    decision_list = list(decisions)
    user_decisions = [
        item for item in decision_list if item.origin is not DecisionOrigin.SYSTEM_OPERATIONAL
    ]

    if user_decisions:
        winner = max(
            user_decisions,
            key=lambda item: (_DECISION_PRECEDENCE[item.origin], item.sequence),
        )
        return EffectiveAssignment(winner.decision, winner.origin, False, winner.decision_id)

    if proposal_list:
        ordered = sorted(proposal_list, key=lambda item: item.confidence, reverse=True)
        winner = ordered[0]
        tied_conflict = any(
            item.confidence == winner.confidence and item.decision is not winner.decision
            for item in ordered[1:]
        )
        return EffectiveAssignment(
            AssignmentDecision.ABSTAIN if tied_conflict else winner.decision,
            winner.origin,
            tied_conflict,
            winner.proposal_id,
        )

    if decision_list:
        winner = max(decision_list, key=lambda item: item.sequence)
        return EffectiveAssignment(winner.decision, winner.origin, False, winner.decision_id)
    return None


def authorize_v1_mutation(context: MutationContext) -> AuthorizationDecision:
    reasons: list[str] = []
    if context.mutation not in _ALLOWED_V1_MUTATIONS:
        reasons.append("mutation kind is prohibited in V1")

    if context.mutation is MutationKind.ADD_ITEMS:
        if context.playlist_role is not PlaylistRole.MANAGED:
            reasons.append("target is not explicitly registered as managed")
        if not context.stable_playlist_id_registered:
            reasons.append("stable target ID is not registered")
        if not context.target_exists:
            reasons.append("registered target no longer exists")
        if not context.live_owner_validated:
            reasons.append("live ownership has not been validated")

    if context.mutation is MutationKind.CREATE_PLAYLIST and context.target_exists:
        reasons.append("creation target already exists")
    if not context.dry_run_approved:
        reasons.append("dry-run has not been approved")
    if not context.change_set_approved:
        reasons.append("change set has not been approved")
    return AuthorizationDecision(not reasons, tuple(reasons))


def is_explicit_calibration_example(
    decision: UserDecision, explicitly_selected: bool
) -> bool:
    return explicitly_selected and decision.origin in {
        DecisionOrigin.USER_APPROVAL,
        DecisionOrigin.USER_OVERRIDE,
        DecisionOrigin.USER_EXPLICIT,
    }


def validate_proposal_references(
    proposal: ClassificationProposal,
    *,
    known_evidence_ids: set[str],
    known_characteristic_ids: set[str],
    known_playlist_ids: set[str],
) -> None:
    if proposal.logical_playlist_id not in known_playlist_ids:
        raise ValueError("proposal references an unknown logical playlist")
    if not set(proposal.evidence_ids).issubset(known_evidence_ids):
        raise ValueError("proposal references unknown evidence")
    if not set(proposal.characteristic_ids).issubset(known_characteristic_ids):
        raise ValueError("proposal references unknown characteristics")
