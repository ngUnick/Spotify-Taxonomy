"""Resumable bounded population runs over a previously imported classification file."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256

from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.persistence.database import utc_now
from spotify_playlist_taxonomy.ports.music_platform import MusicPlatformReader
from spotify_playlist_taxonomy.workflows.phase11 import (
    BatchPopulationOutcome,
    BatchPopulationPreview,
    ManagedTrackAdder,
    Phase11Service,
)


CATALOGUE_COMPLETION_RUN_VERSION = "catalogue-completion-run.v1"


@dataclass(frozen=True, slots=True)
class CatalogueCompletionRun:
    run_id: str
    canonical_ids: tuple[str, ...]
    input_digest: str
    batch_plan_id: str
    state: str
    retry_after_seconds: int | None
    last_reason: str | None


class CatalogueCompletionService:
    """Persists only bounded run progress; Phase 11 remains the sole writer path."""

    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.population = Phase11Service(connection, bundle)

    def preview(self, canonical_ids: tuple[str, ...], input_digest: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader) -> tuple[CatalogueCompletionRun, BatchPopulationPreview]:
        canonical_ids = self.population._batch_canonical_ids(canonical_ids)
        plan = self.population.batch_preview(canonical_ids, account_binding_id=account_binding_id, account_id=account_id, scopes=scopes, reader=reader)
        return self._run(canonical_ids, input_digest, plan.plan_id, "started"), plan

    def apply(self, canonical_ids: tuple[str, ...], input_digest: str, confirmation: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader, adder: ManagedTrackAdder) -> tuple[CatalogueCompletionRun, BatchPopulationOutcome]:
        canonical_ids = self.population._batch_canonical_ids(canonical_ids)
        plan_id = self.population.batch_plan_id(canonical_ids)
        run = self._run(canonical_ids, input_digest, plan_id, "started")
        if confirmation != f"APPLY:{run.run_id}":
            raise ValueError("explicit confirmation must equal APPLY:<catalogue-run-id>")
        self._upsert(run)
        try:
            outcome = self.population.batch_apply(canonical_ids, f"APPLY:{plan_id}", account_binding_id=account_binding_id, account_id=account_id, scopes=scopes, reader=reader, adder=adder)
        except SpotifyAdapterError as error:
            if error.kind is SpotifyErrorKind.QUOTA_EXHAUSTED:
                run = self._replace(run, "paused_quota", str(error), error.retry_after_seconds)
                self._upsert(run)
            raise
        state, reason, retry_after_seconds = self._outcome_state(outcome)
        run = self._replace(run, state, reason, retry_after_seconds)
        self._upsert(run)
        return run, outcome

    def resume(self, run_id: str, confirmation: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader, adder: ManagedTrackAdder) -> tuple[CatalogueCompletionRun, BatchPopulationOutcome]:
        run = self.load(run_id)
        if confirmation != f"RESUME:{run_id}":
            raise ValueError("explicit confirmation must equal RESUME:<catalogue-run-id>")
        if run.state == "recovery_required" and not self._recovery_verified(run):
            raise ValueError("run contains an ambiguous outbound result; use explicit population recovery verification")
        current_plan_id = self.population.batch_plan_id(run.canonical_ids)
        if current_plan_id != run.batch_plan_id:
            raise ValueError("run plan changed; create a new catalogue run preview before applying")
        try:
            outcome = self.population.batch_apply(run.canonical_ids, f"APPLY:{current_plan_id}", account_binding_id=account_binding_id, account_id=account_id, scopes=scopes, reader=reader, adder=adder)
        except SpotifyAdapterError as error:
            if error.kind is SpotifyErrorKind.QUOTA_EXHAUSTED:
                run = self._replace(run, "paused_quota", str(error), error.retry_after_seconds)
                self._upsert(run)
            raise
        state, reason, retry_after_seconds = self._outcome_state(outcome)
        run = self._replace(run, state, reason, retry_after_seconds)
        self._upsert(run)
        return run, outcome

    def load(self, run_id: str) -> CatalogueCompletionRun:
        row = self.connection.execute("SELECT * FROM catalogue_completion_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("unknown catalogue completion run")
        try:
            values = json.loads(row["canonical_ids_json"])
        except (TypeError, json.JSONDecodeError):
            raise ValueError("catalogue completion run is invalid") from None
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError("catalogue completion run is invalid")
        return CatalogueCompletionRun(run_id, tuple(values), row["input_digest"], row["batch_plan_id"], row["state"], row["retry_after_seconds"], row["last_reason"])

    @staticmethod
    def input_digest(raw: object) -> str:
        return sha256(json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()

    def _run(self, canonical_ids: tuple[str, ...], input_digest: str, batch_plan_id: str, state: str) -> CatalogueCompletionRun:
        payload = json.dumps({"version": CATALOGUE_COMPLETION_RUN_VERSION, "canonical_ids": canonical_ids, "input_digest": input_digest, "batch_plan_id": batch_plan_id}, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return CatalogueCompletionRun(sha256(payload).hexdigest()[:16], canonical_ids, input_digest, batch_plan_id, state, None, None)

    @staticmethod
    def _replace(run: CatalogueCompletionRun, state: str, reason: str | None, retry_after_seconds: int | None) -> CatalogueCompletionRun:
        return CatalogueCompletionRun(run.run_id, run.canonical_ids, run.input_digest, run.batch_plan_id, state, retry_after_seconds, reason)

    @staticmethod
    def _outcome_state(outcome: BatchPopulationOutcome) -> tuple[str, str | None, int | None]:
        if outcome.count("recovery_required"):
            return "recovery_required", "an ambiguous outbound addition requires explicit recovery verification", None
        if any(item.state == "blocked" and "quota" in item.reason.lower() for item in outcome.entries):
            return "paused_quota", "provider quota exhausted before a later write; resume this run when permitted", outcome.retry_after_seconds
        return "complete", None, None

    def _recovery_verified(self, run: CatalogueCompletionRun) -> bool:
        """A recovery run may continue only after durable exact read-back resolution."""

        if not run.canonical_ids:
            return False
        marks = ",".join("?" for _ in run.canonical_ids)
        unresolved = self.connection.execute(
            f"SELECT 1 FROM managed_target_addition_attempts WHERE canonical_track_id IN ({marks}) "
            "AND state IN ('started', 'recovery_required') LIMIT 1", run.canonical_ids,
        ).fetchone()
        if unresolved is not None:
            return False
        resolved = self.connection.execute(
            f"SELECT 1 FROM managed_target_addition_attempts WHERE canonical_track_id IN ({marks}) "
            "AND state='confirmed_recovered' LIMIT 1", run.canonical_ids,
        ).fetchone()
        return resolved is not None

    def _upsert(self, run: CatalogueCompletionRun) -> None:
        try:
            now = utc_now()
            self.connection.execute(
                "INSERT INTO catalogue_completion_runs (run_id, canonical_ids_json, input_digest, batch_plan_id, state, last_reason, retry_after_seconds, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET state=excluded.state, last_reason=excluded.last_reason, retry_after_seconds=excluded.retry_after_seconds, updated_at=excluded.updated_at",
                (run.run_id, json.dumps(run.canonical_ids, separators=(",", ":")), run.input_digest, run.batch_plan_id, run.state, run.last_reason, run.retry_after_seconds, now, now),
            )
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ValueError("catalogue completion run could not be saved") from error
