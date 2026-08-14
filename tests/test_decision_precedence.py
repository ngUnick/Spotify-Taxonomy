import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.domain.models import (
    AssignmentDecision,
    ClassificationProposal,
    DecisionOrigin,
    ProposalOrigin,
    ReviewStatus,
    UserDecision,
)
from spotify_playlist_taxonomy.domain.policies import (
    is_explicit_calibration_example,
    resolve_effective_assignment,
)


def proposal(decision: AssignmentDecision, confidence: float = 0.95) -> ClassificationProposal:
    return ClassificationProposal(
        proposal_id=f"proposal.synthetic.{decision.value}",
        canonical_track_id="track.synthetic.001",
        logical_playlist_id="activity.coding_focus",
        decision=decision,
        confidence=confidence,
        origin=ProposalOrigin.CODEX_CLASSIFIER,
        evidence_ids=("evidence.synthetic.001",),
        characteristic_ids=(),
        justification_summary="Synthetic evidence supports this compact proposal.",
        taxonomy_version="taxonomy.synthetic.v1",
        specification_version="spec.synthetic.v1",
        review_status=ReviewStatus.PROPOSED,
    )


def decision(origin: DecisionOrigin, value: AssignmentDecision, sequence: int) -> UserDecision:
    return UserDecision(
        decision_id=f"decision.synthetic.{origin.value}.{sequence}",
        canonical_track_id="track.synthetic.001",
        logical_playlist_id="activity.coding_focus",
        decision=value,
        origin=origin,
        sequence=sequence,
    )


class DecisionPrecedenceTests(unittest.TestCase):
    def test_user_approval_outweighs_generated_proposal(self) -> None:
        result = resolve_effective_assignment(
            [proposal(AssignmentDecision.INCLUDE)],
            [decision(DecisionOrigin.USER_APPROVAL, AssignmentDecision.EXCLUDE, 1)],
        )
        self.assertEqual(AssignmentDecision.EXCLUDE, result.decision)
        self.assertEqual(DecisionOrigin.USER_APPROVAL, result.origin)

    def test_system_operational_state_does_not_override_generated_proposal(self) -> None:
        result = resolve_effective_assignment(
            [proposal(AssignmentDecision.INCLUDE)],
            [decision(DecisionOrigin.SYSTEM_OPERATIONAL, AssignmentDecision.EXCLUDE, 99)],
        )
        self.assertEqual(AssignmentDecision.INCLUDE, result.decision)
        self.assertEqual(ProposalOrigin.CODEX_CLASSIFIER, result.origin)

    def test_explicit_override_outweighs_later_approval(self) -> None:
        result = resolve_effective_assignment(
            [proposal(AssignmentDecision.INCLUDE)],
            [
                decision(DecisionOrigin.USER_OVERRIDE, AssignmentDecision.EXCLUDE, 1),
                decision(DecisionOrigin.USER_APPROVAL, AssignmentDecision.INCLUDE, 99),
            ],
        )
        self.assertEqual(AssignmentDecision.EXCLUDE, result.decision)
        self.assertEqual(DecisionOrigin.USER_OVERRIDE, result.origin)

    def test_latest_same_precedence_decision_wins(self) -> None:
        result = resolve_effective_assignment(
            [],
            [
                decision(DecisionOrigin.USER_EXPLICIT, AssignmentDecision.EXCLUDE, 4),
                decision(DecisionOrigin.USER_EXPLICIT, AssignmentDecision.INCLUDE, 5),
            ],
        )
        self.assertEqual(AssignmentDecision.INCLUDE, result.decision)

    def test_conflicting_equal_confidence_proposals_need_review(self) -> None:
        result = resolve_effective_assignment(
            [
                proposal(AssignmentDecision.INCLUDE, 0.7),
                proposal(AssignmentDecision.EXCLUDE, 0.7),
            ],
            [],
        )
        self.assertEqual(AssignmentDecision.ABSTAIN, result.decision)
        self.assertTrue(result.needs_review)

    def test_calibration_requires_explicit_selection_and_reviewed_origin(self) -> None:
        reviewed = decision(DecisionOrigin.USER_APPROVAL, AssignmentDecision.INCLUDE, 1)
        operational = decision(DecisionOrigin.SYSTEM_OPERATIONAL, AssignmentDecision.INCLUDE, 2)
        self.assertTrue(is_explicit_calibration_example(reviewed, True))
        self.assertFalse(is_explicit_calibration_example(reviewed, False))
        self.assertFalse(is_explicit_calibration_example(operational, True))


if __name__ == "__main__":
    unittest.main()
