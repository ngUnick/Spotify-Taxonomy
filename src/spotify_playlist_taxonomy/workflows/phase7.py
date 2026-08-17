"""Offline user review, effective-state, and observational calibration workflow."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, DecisionOrigin
from spotify_playlist_taxonomy.persistence.database import utc_now


@dataclass(frozen=True, slots=True)
class EffectiveReview:
    canonical_track_id: str
    logical_playlist_id: str
    generated: str | None
    explicit: str | None
    effective: str | None
    needs_review: bool
    stale: bool


class Phase7Service:
    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.specifications = {x.logical_playlist_id: x for x in bundle.specifications}

    def _proposal(self, canonical_id: str, logical_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM phase6_proposals WHERE canonical_track_id=? AND logical_playlist_id=? ORDER BY created_at DESC LIMIT 1", (canonical_id, logical_id)).fetchone()

    def _latest_event(self, canonical_id: str, logical_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM phase7_review_events WHERE canonical_track_id=? AND logical_playlist_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (canonical_id, logical_id)).fetchone()

    def effective(self, canonical_id: str, logical_id: str) -> EffectiveReview:
        proposal, event = self._proposal(canonical_id, logical_id), self._latest_event(canonical_id, logical_id)
        explicit = event["decision"] if event is not None and event["action"] == "decide" else None
        stale = bool(explicit and proposal and event["specification_version"] != proposal["specification_version"])
        generated = proposal["decision"] if proposal is not None else None
        return EffectiveReview(canonical_id, logical_id, generated, explicit, explicit or generated, stale or (proposal is not None and proposal["proposal_state"] == "conflicting"), stale)

    def decide(self, canonical_id: str, logical_id: str, decision: AssignmentDecision, note: str | None = None) -> EffectiveReview:
        spec = self.specifications.get(logical_id)
        if spec is None: raise ValueError("unknown reviewed semantic specification")
        if note is not None and (not note.strip() or len(note) > 280): raise ValueError("note must be nonblank and <= 280 characters")
        current = self._latest_event(canonical_id, logical_id)
        if current is not None and current["action"] == "decide" and current["decision"] == decision.value and current["specification_version"] == spec.specification_version and current["note"] == note:
            return self.effective(canonical_id, logical_id)
        proposal = self._proposal(canonical_id, logical_id)
        self.connection.execute("INSERT INTO phase7_review_events VALUES (?, ?, ?, ?, 'decide', ?, 'user_explicit', ?, ?, ?, ?, ?)",
            (f"review.{uuid.uuid4().hex}", canonical_id, logical_id, proposal["proposal_id"] if proposal else None, decision.value, self.bundle.taxonomy.taxonomy_version, spec.specification_version, self.bundle.digest, note, utc_now()))
        self.connection.commit(); return self.effective(canonical_id, logical_id)

    def clear(self, canonical_id: str, logical_id: str) -> EffectiveReview:
        current = self._latest_event(canonical_id, logical_id)
        if current is None or current["action"] == "clear": return self.effective(canonical_id, logical_id)
        spec=self.specifications.get(logical_id)
        if spec is None: raise ValueError("unknown reviewed semantic specification")
        self.connection.execute("INSERT INTO phase7_review_events VALUES (?, ?, ?, NULL, 'clear', NULL, 'user_explicit', ?, ?, ?, NULL, ?)",
            (f"review.{uuid.uuid4().hex}", canonical_id, logical_id, self.bundle.taxonomy.taxonomy_version, spec.specification_version, self.bundle.digest, utc_now()))
        self.connection.commit(); return self.effective(canonical_id, logical_id)

    def show(self, canonical_id: str) -> tuple[EffectiveReview, ...]:
        ids={row[0] for row in self.connection.execute("SELECT logical_playlist_id FROM phase6_proposals WHERE canonical_track_id=?", (canonical_id,))}
        ids |= {row[0] for row in self.connection.execute("SELECT logical_playlist_id FROM phase7_review_events WHERE canonical_track_id=?", (canonical_id,))}
        return tuple(self.effective(canonical_id, key) for key in sorted(ids))

    def queue(self, limit: int = 25) -> tuple[EffectiveReview, ...]:
        rows=self.connection.execute("SELECT DISTINCT canonical_track_id, logical_playlist_id FROM phase6_proposals WHERE proposal_state='conflicting' ORDER BY canonical_track_id, logical_playlist_id LIMIT ?", (limit,))
        return tuple(self.effective(row[0], row[1]) for row in rows)

    def history(self, canonical_id: str, logical_id: str | None = None) -> tuple[sqlite3.Row, ...]:
        query="SELECT * FROM phase7_review_events WHERE canonical_track_id=?"; args=[canonical_id]
        if logical_id: query += " AND logical_playlist_id=?"; args.append(logical_id)
        return tuple(self.connection.execute(query + " ORDER BY created_at, rowid", args))

    def calibration(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute("""
            SELECT p.logical_playlist_id, count(*) AS reviewed,
              sum(CASE WHEN e.decision=p.decision THEN 1 ELSE 0 END) AS agreement,
              sum(CASE WHEN p.decision='include' AND e.decision='exclude' THEN 1 ELSE 0 END) AS include_rejected,
              sum(CASE WHEN p.decision IN ('abstain','exclude') AND e.decision='include' THEN 1 ELSE 0 END) AS noninclude_approved
            FROM phase7_review_events e JOIN phase6_proposals p ON p.proposal_id=e.proposal_id
            WHERE e.action='decide' GROUP BY p.logical_playlist_id ORDER BY p.logical_playlist_id
        """))
