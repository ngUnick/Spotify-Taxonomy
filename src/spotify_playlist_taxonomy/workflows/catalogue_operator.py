"""Local, deterministic discovery of the next safe catalogue-operator action."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from spotify_playlist_taxonomy.workflows.catalogue_completion import CatalogueCompletionRun


@dataclass(frozen=True, slots=True)
class CatalogueRecoveryIdentity:
    output_target_id: str
    canonical_track_id: str

    @property
    def confirmation(self) -> str:
        return f"RECOVER:{self.output_target_id}:{self.canonical_track_id}"


@dataclass(frozen=True, slots=True)
class CatalogueOperatorStatus:
    """Only durable local facts; no provider preflight or mutation is performed."""

    run: CatalogueCompletionRun | None
    next_action: str
    next_confirmation: str | None
    recovery: CatalogueRecoveryIdentity | None
    run_recovery_identity_count: int
    other_population_recovery_count: int
    durable_confirmed_additions: int
    durable_recovery_required_additions: int
    unreviewed_classification_canonicals: int
    managed_provisioning_recovery_count: int


class CatalogueOperatorService:
    """Projects persisted run and recovery state with a safety-first priority order."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def status(self) -> CatalogueOperatorStatus:
        run = self._current_run()
        all_recoveries = tuple(self.connection.execute(
            "SELECT output_target_id, canonical_track_id FROM managed_target_addition_attempts "
            "WHERE state IN ('started', 'recovery_required') ORDER BY created_at DESC, attempt_id DESC"
        ))
        run_recoveries = self._run_recoveries(run, all_recoveries)
        if run is not None and run.state == "recovery_required":
            primary_recoveries = run_recoveries
        else:
            primary_recoveries = all_recoveries
        recovery = (
            CatalogueRecoveryIdentity(primary_recoveries[0]["output_target_id"], primary_recoveries[0]["canonical_track_id"])
            if len(primary_recoveries) == 1 else None
        )
        confirmed, unresolved = self._durable_addition_counts(run)
        unreviewed = self._unreviewed_count()
        managed_recovery = self.connection.execute(
            "SELECT COUNT(*) FROM managed_target_registrations WHERE state='recovery_required'"
        ).fetchone()[0]

        if primary_recoveries:
            # Multiple records are real but no single recovery command can be safely guessed.
            if recovery is None:
                action, confirmation = "blocked_manual", None
            else:
                action, confirmation = "recovery_verify", recovery.confirmation
        elif run is not None and run.state == "recovery_required":
            if all_recoveries:
                # The run is resolved, but another global addition recovery still has safety priority.
                action, confirmation = self._global_recovery_action(all_recoveries)
            elif self._recovery_verified(run):
                action, confirmation = "resume_population", f"RESUME:{run.run_id}"
            else:
                # The run records an unresolved outcome but no exact durable verified recovery exists.
                action, confirmation = "blocked_manual", None
        elif managed_recovery:
            action, confirmation = "blocked_manual", None
        elif run is not None:
            action, confirmation = "resume_population", f"RESUME:{run.run_id}"
        elif unreviewed:
            action, confirmation = "generate_worklist", None
        else:
            action, confirmation = "complete", None
        return CatalogueOperatorStatus(
            run, action, confirmation, recovery, len(run_recoveries), len(all_recoveries) - len(run_recoveries), confirmed, unresolved,
            unreviewed, managed_recovery,
        )

    @staticmethod
    def _global_recovery_action(recoveries: tuple[sqlite3.Row, ...]) -> tuple[str, str | None]:
        if len(recoveries) != 1:
            return "blocked_manual", None
        item = CatalogueRecoveryIdentity(recoveries[0]["output_target_id"], recoveries[0]["canonical_track_id"])
        return "recovery_verify", item.confirmation

    @staticmethod
    def _run_recoveries(
        run: CatalogueCompletionRun | None, recoveries: tuple[sqlite3.Row, ...],
    ) -> tuple[sqlite3.Row, ...]:
        if run is None:
            return ()
        run_canonical_ids = set(run.canonical_ids)
        return tuple(item for item in recoveries if item["canonical_track_id"] in run_canonical_ids)

    def _current_run(self) -> CatalogueCompletionRun | None:
        row = self.connection.execute(
            "SELECT * FROM catalogue_completion_runs WHERE state IN ('recovery_required', 'paused_quota', 'started') "
            "ORDER BY CASE state WHEN 'recovery_required' THEN 0 WHEN 'paused_quota' THEN 1 ELSE 2 END, "
            "updated_at DESC, run_id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            canonical_ids = json.loads(row["canonical_ids_json"])
        except (TypeError, json.JSONDecodeError):
            # A corrupt run cannot be resumed and must be surfaced as a structural CLI error.
            raise ValueError("catalogue completion run is invalid") from None
        if not isinstance(canonical_ids, list) or not all(isinstance(item, str) for item in canonical_ids):
            raise ValueError("catalogue completion run is invalid")
        return CatalogueCompletionRun(
            row["run_id"], tuple(canonical_ids), row["input_digest"], row["batch_plan_id"], row["state"],
            row["retry_after_seconds"], row["last_reason"],
        )

    def _durable_addition_counts(self, run: CatalogueCompletionRun | None) -> tuple[int, int]:
        if run is None or not run.canonical_ids:
            return 0, 0
        marks = ",".join("?" for _ in run.canonical_ids)
        rows = self.connection.execute(
            f"SELECT state, COUNT(*) AS count FROM managed_target_addition_attempts "
            f"WHERE canonical_track_id IN ({marks}) GROUP BY state", run.canonical_ids,
        )
        counts = {row["state"]: row["count"] for row in rows}
        return (
            counts.get("confirmed_added", 0) + counts.get("confirmed_recovered", 0),
            counts.get("started", 0) + counts.get("recovery_required", 0),
        )

    def _recovery_verified(self, run: CatalogueCompletionRun) -> bool:
        if not run.canonical_ids:
            return False
        marks = ",".join("?" for _ in run.canonical_ids)
        return self.connection.execute(
            f"SELECT 1 FROM managed_target_addition_attempts WHERE canonical_track_id IN ({marks}) "
            "AND state='confirmed_recovered' LIMIT 1", run.canonical_ids,
        ).fetchone() is not None

    def _unreviewed_count(self) -> int:
        # This intentionally matches the worklist's semantic-review-event boundary: a routing event alone
        # never marks a canonical as reviewed.
        return self.connection.execute(
            "SELECT COUNT(*) FROM canonical_tracks AS canonical WHERE NOT EXISTS ("
            "SELECT 1 FROM phase7_review_events AS review "
            "WHERE review.canonical_track_id=canonical.canonical_track_id AND review.action='decide')"
        ).fetchone()[0]
