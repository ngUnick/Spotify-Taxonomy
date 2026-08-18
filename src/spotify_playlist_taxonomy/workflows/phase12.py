"""Local-only V1 operational status over existing authoritative state."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase9 import Phase9Service

@dataclass(frozen=True, slots=True)
class CycleCategory:
    name: str; count: int; identifiers: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class CycleReport:
    account_state: str; categories: tuple[CycleCategory, ...]

class Phase12Service:
    """Read-only status projection; it never invokes providers or mutates SQLite."""
    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle; self.review, self.phase9 = Phase7Service(connection, bundle), Phase9Service(connection, bundle)
    def status(self, limit: int = 10) -> CycleReport:
        if not 1 <= limit <= 100: raise ValueError("cycle status limit must be between 1 and 100")
        active = self.connection.execute("SELECT 1 FROM platform_account_bindings WHERE active=1").fetchone()
        categories = (self._sql("active_inbox_sources", "SELECT source_id FROM ingestion_sources WHERE active=1 AND role='inbox' ORDER BY source_id", limit), self._sql("incomplete_ingestion", "SELECT scan_id FROM source_scans WHERE status IN ('pending','running','failed','stale') ORDER BY scan_id", limit), self._sql("match_review", "SELECT candidate_id FROM canonical_match_candidates WHERE state='reviewable' ORDER BY candidate_id", limit), self._sql("research_actionable", "SELECT DISTINCT canonical_track_id FROM research_jobs WHERE status IN ('running','partial','failed') ORDER BY canonical_track_id", limit), self._sql("codex_awaiting_result", "SELECT batch_id FROM codex_batches WHERE state='awaiting_result' ORDER BY batch_id", limit), self._effective_review(limit), self._desired(limit), self._sql("provisioning_recovery", "SELECT output_target_id FROM managed_target_registrations WHERE state='recovery_required' ORDER BY output_target_id", limit), self._sql("addition_recovery", "SELECT attempt_id FROM managed_target_addition_attempts WHERE state IN ('started','recovery_required') ORDER BY attempt_id", limit))
        return CycleReport("bound" if active else "no_active_binding", categories)
    def _sql(self, name: str, query: str, limit: int) -> CycleCategory:
        rows = tuple(str(row[0]) for row in self.connection.execute(query)); return CycleCategory(name, len(rows), rows[:limit])
    def _effective_review(self, limit: int) -> CycleCategory:
        rows = self.connection.execute("SELECT canonical_track_id, logical_playlist_id FROM phase6_proposals UNION SELECT canonical_track_id, logical_playlist_id FROM phase8_codex_proposals ORDER BY canonical_track_id, logical_playlist_id")
        values = tuple(f"{row[0]}:{row[1]}" for row in rows if self.review.effective(row[0], row[1]).needs_review); return CycleCategory("review_actionable", len(values), values[:limit])
    def _desired(self, limit: int) -> CycleCategory:
        ids = tuple(row[0] for row in self.connection.execute("SELECT DISTINCT canonical_track_id FROM phase6_proposals UNION SELECT DISTINCT canonical_track_id FROM phase8_codex_proposals ORDER BY canonical_track_id")); unresolved=[]; unprovisioned=[]; registered={row[0] for row in self.connection.execute("SELECT output_target_id FROM managed_target_registrations WHERE state='active' AND policy_confirmed=1")}
        for canonical_id in ids:
            desired=self.phase9.desired(canonical_id); unresolved.extend(f"routing:{item.canonical_track_id}:{item.semantic_specification_id}" for item in desired.unresolved); unprovisioned.extend(f"unprovisioned:{item.output_target_id}" for item in desired.memberships if item.output_target_id not in registered)
        values=tuple(sorted(set(unresolved)|set(unprovisioned))); return CycleCategory("routing_or_unprovisioned", len(values), values[:limit])
