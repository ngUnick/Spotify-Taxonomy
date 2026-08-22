"""Offline user review and effective-state aggregation for generated proposals."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision
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
    generated_sources: tuple[str, ...] = ()
    generated_disagreement: bool = False


class Phase7Service:
    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.specifications = {item.logical_playlist_id: item for item in bundle.specifications}

    def _generated(self, canonical_id: str, logical_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute(
            """
            SELECT proposal_id, decision, proposal_state, specification_version, 'deterministic' AS source
            FROM phase6_proposals WHERE canonical_track_id=? AND logical_playlist_id=?
            UNION ALL
            SELECT proposal_id, decision, 'generated' AS proposal_state, specification_version, 'codex' AS source
            FROM phase8_codex_proposals WHERE canonical_track_id=? AND logical_playlist_id=?
            ORDER BY source, proposal_id
            """,
            (canonical_id, logical_id, canonical_id, logical_id),
        ))

    def _latest_event(self, canonical_id: str, logical_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM phase7_review_events WHERE canonical_track_id=? AND logical_playlist_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (canonical_id, logical_id),
        ).fetchone()

    def effective(self, canonical_id: str, logical_id: str) -> EffectiveReview:
        proposals = self._generated(canonical_id, logical_id)
        event = self._latest_event(canonical_id, logical_id)
        return self._effective_from_rows(canonical_id, logical_id, proposals, event)

    def _effective_from_rows(
        self, canonical_id: str, logical_id: str, proposals: tuple[sqlite3.Row, ...],
        event: sqlite3.Row | None,
    ) -> EffectiveReview:
        explicit = event["decision"] if event is not None and event["action"] == "decide" else None
        decisions = {proposal["decision"] for proposal in proposals}
        disagreement = len(decisions) > 1
        generated = next(iter(decisions)) if len(decisions) == 1 else None
        stale = bool(
            explicit and proposals and any(
                event["specification_version"] != proposal["specification_version"]
                for proposal in proposals
            )
        )
        conflict = any(proposal["proposal_state"] == "conflicting" for proposal in proposals)
        return EffectiveReview(
            canonical_id,
            logical_id,
            generated,
            explicit,
            explicit or generated,
            stale or conflict or disagreement,
            stale,
            tuple(proposal["source"] for proposal in proposals),
            disagreement,
        )

    def effective_many(
        self, canonical_ids: tuple[str, ...] | None = None,
    ) -> dict[tuple[str, str], EffectiveReview]:
        """Project all requested effective states with shared Phase 7 precedence semantics."""

        wanted = None if canonical_ids is None else set(canonical_ids)
        generated: dict[tuple[str, str], list[sqlite3.Row]] = {}
        rows = self.connection.execute(
            """
            SELECT canonical_track_id, logical_playlist_id, proposal_id, decision,
                   proposal_state, specification_version, 'deterministic' AS source
            FROM phase6_proposals
            UNION ALL
            SELECT canonical_track_id, logical_playlist_id, proposal_id, decision,
                   'generated' AS proposal_state, specification_version, 'codex' AS source
            FROM phase8_codex_proposals
            ORDER BY canonical_track_id, logical_playlist_id, source, proposal_id
            """
        )
        for row in rows:
            if wanted is None or row["canonical_track_id"] in wanted:
                generated.setdefault((row["canonical_track_id"], row["logical_playlist_id"]), []).append(row)
        events: dict[tuple[str, str], sqlite3.Row] = {}
        for row in self.connection.execute(
            "SELECT * FROM phase7_review_events ORDER BY canonical_track_id, logical_playlist_id, created_at DESC, rowid DESC"
        ):
            key = (row["canonical_track_id"], row["logical_playlist_id"])
            if (wanted is None or key[0] in wanted) and key not in events:
                events[key] = row
        keys = set(generated) | set(events)
        return {
            key: self._effective_from_rows(key[0], key[1], tuple(generated.get(key, ())), events.get(key))
            for key in keys
        }

    def decide(
        self, canonical_id: str, logical_id: str, decision: AssignmentDecision, note: str | None = None
    ) -> EffectiveReview:
        spec = self.specifications.get(logical_id)
        if spec is None:
            raise ValueError("unknown reviewed semantic specification")
        if note is not None and (not note.strip() or len(note) > 280):
            raise ValueError("note must be nonblank and <= 280 characters")
        current = self._latest_event(canonical_id, logical_id)
        if current is not None and current["action"] == "decide" and current["decision"] == decision.value and current["specification_version"] == spec.specification_version and current["note"] == note:
            return self.effective(canonical_id, logical_id)
        proposals = self._generated(canonical_id, logical_id)
        deterministic = next((proposal for proposal in proposals if proposal["source"] == "deterministic"), None)
        self.connection.execute(
            "INSERT INTO phase7_review_events VALUES (?, ?, ?, ?, 'decide', ?, 'user_explicit', ?, ?, ?, ?, ?)",
            (
                f"review.{uuid.uuid4().hex}", canonical_id, logical_id,
                deterministic["proposal_id"] if deterministic else None, decision.value,
                self.bundle.taxonomy.taxonomy_version, spec.specification_version,
                self.bundle.digest, note, utc_now(),
            ),
        )
        self.connection.commit()
        return self.effective(canonical_id, logical_id)

    def clear(self, canonical_id: str, logical_id: str) -> EffectiveReview:
        current = self._latest_event(canonical_id, logical_id)
        if current is None or current["action"] == "clear":
            return self.effective(canonical_id, logical_id)
        spec = self.specifications.get(logical_id)
        if spec is None:
            raise ValueError("unknown reviewed semantic specification")
        self.connection.execute(
            "INSERT INTO phase7_review_events VALUES (?, ?, ?, NULL, 'clear', NULL, 'user_explicit', ?, ?, ?, NULL, ?)",
            (f"review.{uuid.uuid4().hex}", canonical_id, logical_id, self.bundle.taxonomy.taxonomy_version, spec.specification_version, self.bundle.digest, utc_now()),
        )
        self.connection.commit()
        return self.effective(canonical_id, logical_id)

    def show(self, canonical_id: str) -> tuple[EffectiveReview, ...]:
        ids = {row[0] for row in self.connection.execute("SELECT logical_playlist_id FROM phase6_proposals WHERE canonical_track_id=?", (canonical_id,))}
        ids |= {row[0] for row in self.connection.execute("SELECT logical_playlist_id FROM phase8_codex_proposals WHERE canonical_track_id=?", (canonical_id,))}
        ids |= {row[0] for row in self.connection.execute("SELECT logical_playlist_id FROM phase7_review_events WHERE canonical_track_id=?", (canonical_id,))}
        return tuple(self.effective(canonical_id, key) for key in sorted(ids))

    def queue(self, limit: int = 25) -> tuple[EffectiveReview, ...]:
        rows = self.connection.execute(
            """
            SELECT canonical_track_id, logical_playlist_id FROM phase6_proposals
            UNION
            SELECT canonical_track_id, logical_playlist_id FROM phase8_codex_proposals
            ORDER BY canonical_track_id, logical_playlist_id
            """
        )
        return tuple(
            review for review in (self.effective(row[0], row[1]) for row in rows)
            if review.needs_review
        )[:limit]

    def history(self, canonical_id: str, logical_id: str | None = None) -> tuple[sqlite3.Row, ...]:
        query = "SELECT * FROM phase7_review_events WHERE canonical_track_id=?"
        args: list[str] = [canonical_id]
        if logical_id:
            query += " AND logical_playlist_id=?"
            args.append(logical_id)
        return tuple(self.connection.execute(query + " ORDER BY created_at, rowid", args))

    def calibration(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute(
            """
            SELECT p.logical_playlist_id, count(*) AS reviewed,
              sum(CASE WHEN e.decision=p.decision THEN 1 ELSE 0 END) AS agreement,
              sum(CASE WHEN p.decision='include' AND e.decision='exclude' THEN 1 ELSE 0 END) AS include_rejected,
              sum(CASE WHEN p.decision IN ('abstain','exclude') AND e.decision='include' THEN 1 ELSE 0 END) AS noninclude_approved
            FROM phase7_review_events e JOIN phase6_proposals p ON p.proposal_id=e.proposal_id
            WHERE e.action='decide' GROUP BY p.logical_playlist_id ORDER BY p.logical_playlist_id
            """
        ))
