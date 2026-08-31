"""Conservative local preview and explicit approval of current Phase 6 proposals."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256

from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision
from spotify_playlist_taxonomy.workflows.genre_routing import GenreRoutingService
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase7_batch import ReviewBatchDecision


BULK_REVIEW_PLAN_VERSION = "deterministic-bulk-review.v1"
BULK_REVIEW_MAX_CANONICALS = 250


@dataclass(frozen=True, slots=True)
class BulkReviewItem:
    canonical_track_id: str
    state: str
    decisions: tuple[ReviewBatchDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class BulkReviewPreview:
    plan_id: str
    items: tuple[BulkReviewItem, ...]

    def count(self, state: str) -> int:
        return sum(item.state == state for item in self.items)


class BulkReviewService:
    """Uses no music heuristics: eligibility is exact current unambiguous Phase 6 state."""

    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.review = Phase7Service(connection, bundle)
        self.routing = GenreRoutingService(connection, bundle)
        self.specifications = {item.logical_playlist_id: item for item in bundle.specifications}

    def preview(self, limit: int = 25) -> BulkReviewPreview:
        if not 1 <= limit <= BULK_REVIEW_MAX_CANONICALS:
            raise ValueError(f"bulk review limit must be between 1 and {BULK_REVIEW_MAX_CANONICALS}")
        items = tuple(self._item(row[0]) for row in self.connection.execute(
            "SELECT canonical_track_id FROM canonical_tracks ORDER BY canonical_track_id LIMIT ?", (limit,)
        ))
        eligible = tuple(item for item in items if item.state == "eligible_for_bulk_approval")
        payload = json.dumps({
            "version": BULK_REVIEW_PLAN_VERSION,
            "bundle_digest": self.bundle.digest,
            "items": [(item.canonical_track_id, tuple((x.semantic_id, x.decision.value) for x in item.decisions)) for item in eligible],
        }, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return BulkReviewPreview(sha256(payload).hexdigest()[:16], items)

    def apply(self, plan_id: str, *, limit: int = 25) -> BulkReviewPreview:
        preview = self.preview(limit)
        if plan_id != preview.plan_id:
            raise ValueError("explicit confirmation must equal APPROVE:<bulk-review-plan-id> for the current preview")
        decisions = tuple(decision for item in preview.items if item.state == "eligible_for_bulk_approval" for decision in item.decisions)
        if not decisions:
            return preview
        # The plan is the explicit operator approval of this exact regenerated deterministic set.
        for decision in decisions:
            self.review._validate_decision(decision.canonical_id, decision.semantic_id, decision.note)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for decision in decisions:
                self.review._record_decision(decision.canonical_id, decision.semantic_id, decision.decision, decision.note)
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ValueError("bulk review approval could not be applied") from error
        return preview

    def _item(self, canonical_id: str) -> BulkReviewItem:
        explicit = self.connection.execute(
            "SELECT 1 FROM phase7_review_events WHERE canonical_track_id=? AND action='decide' LIMIT 1", (canonical_id,)
        ).fetchone()
        if explicit is not None:
            return BulkReviewItem(canonical_id, "manual_review_required")
        proposals = tuple(self.connection.execute(
            "SELECT * FROM phase6_proposals WHERE canonical_track_id=? ORDER BY logical_playlist_id, created_at DESC, rowid DESC", (canonical_id,)
        ))
        latest: dict[str, sqlite3.Row] = {}
        for proposal in proposals:
            latest.setdefault(proposal["logical_playlist_id"], proposal)
        if len(latest) != len(self.specifications):
            return BulkReviewItem(canonical_id, "manual_review_required")
        if len(proposals) != len(self.specifications):
            return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
        if self.connection.execute("SELECT 1 FROM phase8_codex_proposals WHERE canonical_track_id=? LIMIT 1", (canonical_id,)).fetchone():
            return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
        decisions: list[ReviewBatchDecision] = []
        genre_include = False
        for semantic_id, specification in self.specifications.items():
            proposal = latest.get(semantic_id)
            if proposal is None or any((
                proposal["specification_version"] != specification.specification_version,
                proposal["taxonomy_version"] != self.bundle.taxonomy.taxonomy_version,
                proposal["vocabulary_version"] != self.bundle.vocabulary.vocabulary_version,
                proposal["bundle_digest"] != self.bundle.digest,
            )):
                return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
            if proposal["proposal_state"] == "conflicting" or proposal["decision"] == AssignmentDecision.ABSTAIN.value:
                return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
            if proposal["decision"] == AssignmentDecision.INCLUDE.value and proposal["proposal_state"] != "supported":
                return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
            if proposal["decision"] == AssignmentDecision.EXCLUDE.value and proposal["proposal_state"] != "hard_excluded":
                return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
            if proposal["decision"] not in {AssignmentDecision.INCLUDE.value, AssignmentDecision.EXCLUDE.value}:
                return BulkReviewItem(canonical_id, "stale_conflicting_or_ambiguous")
            genre_include = genre_include or (semantic_id.startswith("genre.") and proposal["decision"] == "include")
            decisions.append(ReviewBatchDecision(canonical_id, semantic_id, AssignmentDecision(proposal["decision"]), None))
        if genre_include and self.routing.show(canonical_id).state != "current":
            return BulkReviewItem(canonical_id, "genre_routing_unresolved")
        return BulkReviewItem(canonical_id, "eligible_for_bulk_approval", tuple(decisions))
