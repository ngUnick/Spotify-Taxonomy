"""Transactional SQLite state for Phase 5 research."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack,
    EvidenceKind,
    ResearchEvidence,
    ResearchSource,
)
from spotify_playlist_taxonomy.ports.research import (
    EvidenceType,
    IndependentIdentifier,
    ProviderAssertion,
    ProviderOutcomeStatus,
    QualityBand,
    ResearchResult,
    SubjectMatch,
    SubjectScope,
)

from .database import utc_now
from .store import RecordNotFoundError


@dataclass(frozen=True, slots=True)
class ResearchSubject:
    track: CanonicalTrack
    version_cues: tuple[str, ...]
    independent_identifiers: tuple[IndependentIdentifier, ...]


@dataclass(frozen=True, slots=True)
class CacheRecord:
    cache_key: str
    provider_id: str
    adapter_version: str
    request_schema_version: str
    canonical_track_id: str
    subject_fingerprint: str
    query_fingerprint: str
    outcome: ProviderOutcomeStatus
    producing_attempt_id: str


class ResearchStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def subject(self, canonical_track_id: str) -> ResearchSubject:
        row = self.connection.execute(
            "SELECT canonical_track_id, display_title FROM canonical_tracks WHERE canonical_track_id = ?",
            (canonical_track_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(canonical_track_id)
        artists = tuple(
            item["artist_name"] for item in self.connection.execute(
                "SELECT artist_name FROM canonical_track_artists "
                "WHERE canonical_track_id = ? ORDER BY position",
                (canonical_track_id,),
            )
        )
        identity_rows = self.connection.execute(
            """
            SELECT DISTINCT ti.provider, ti.provider_track_id, ti.version_label
            FROM track_identities AS ti
            JOIN (
                SELECT identity_id FROM canonical_track_identity_links WHERE canonical_track_id = ?
                UNION
                SELECT identity_id FROM account_canonical_identity_links
                WHERE canonical_track_id = ?
            ) AS linked ON linked.identity_id = ti.identity_id
            ORDER BY ti.provider, ti.provider_track_id
            """,
            (canonical_track_id, canonical_track_id),
        ).fetchall()
        cues = tuple(sorted({item["version_label"] for item in identity_rows if item["version_label"]}))
        identifiers = tuple(
            IndependentIdentifier(item["provider"], item["provider_track_id"])
            for item in identity_rows
            if item["provider"] != "spotify" and item["provider_track_id"]
        )
        evidence_identifiers: list[IndependentIdentifier] = []
        for item in self.connection.execute(
            """
            SELECT normalized_value FROM research_evidence
            WHERE canonical_track_id = ? AND evidence_state = 'active'
              AND evidence_type = 'independent_identifier' AND subject_match = 'exact'
            ORDER BY evidence_id
            """,
            (canonical_track_id,),
        ):
            try:
                value = json.loads(item["normalized_value"])
            except (TypeError, ValueError):
                continue
            namespace = value.get("namespace") if isinstance(value, dict) else None
            identifier = value.get("value") if isinstance(value, dict) else None
            if isinstance(namespace, str) and namespace.strip() and isinstance(identifier, str) and identifier.strip():
                evidence_identifiers.append(IndependentIdentifier(namespace, identifier))
        all_identifiers = {
            (item.namespace, item.value): item
            for item in (*identifiers, *evidence_identifiers)
        }
        return ResearchSubject(
            CanonicalTrack(row["canonical_track_id"], row["display_title"], artists),
            cues,
            tuple(all_identifiers[key] for key in sorted(all_identifiers)),
        )

    def create_job(
        self, *, job_id: str, request_id: str, canonical_track_id: str,
        subject_fingerprint: str, policy_version: str, bundle_digest: str,
        requested_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO research_jobs(
                job_id, canonical_track_id, request_id, subject_fingerprint,
                research_policy_version, phase4_bundle_digest, status, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                job_id, canonical_track_id, request_id, subject_fingerprint,
                policy_version, bundle_digest, requested_at,
            ),
        )
        self.connection.commit()

    def finish_job(self, job_id: str, status: str, completed_at: str) -> None:
        cursor = self.connection.execute(
            "UPDATE research_jobs SET status = ?, completed_at = ? "
            "WHERE job_id = ? AND status = 'running'",
            (status, completed_at, job_id),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise RuntimeError("research job is missing or no longer running")
        self.connection.commit()

    def fresh_cache(self, cache_key: str, now: str) -> CacheRecord | None:
        row = self.connection.execute(
            """
            SELECT * FROM research_cache_entries
            WHERE cache_key = ? AND invalidated_at IS NULL
              AND COALESCE(fresh_until, negative_until, '9999-12-31T23:59:59Z') > ?
            """,
            (cache_key, now),
        ).fetchone()
        if row is None:
            return None
        return CacheRecord(
            row["cache_key"], row["provider_id"], row["adapter_version"],
            row["request_schema_version"], row["canonical_track_id"],
            row["subject_fingerprint"], row["query_fingerprint"],
            ProviderOutcomeStatus(row["outcome"]), row["producing_attempt_id"],
        )

    def cached_result(self, record: CacheRecord, request_id: str, retrieved_at: str) -> ResearchResult:
        assertion_rows = self.connection.execute(
            "SELECT * FROM research_provider_assertions WHERE attempt_id = ? ORDER BY assertion_id",
            (record.producing_attempt_id,),
        ).fetchall()
        assertions = tuple(
            ProviderAssertion(
                row["assertion_key"], EvidenceType(row["evidence_type"]),
                json.loads(row["asserted_value_json"]), EvidenceKind(row["evidence_kind"]),
                ResearchSource(row["source"]), row["source_reference"],
                SubjectScope(row["subject_scope"]), SubjectMatch(row["subject_match"]),
                row["native_confidence"], QualityBand(row["quality_band"]),
                row["observed_at"], True,
            )
            for row in assertion_rows
        )
        return ResearchResult(
            request_id, record.provider_id, record.adapter_version,
            f"cache:{record.cache_key}", record.query_fingerprint, retrieved_at,
            record.outcome, assertions,
        )

    def record_cache_hit(
        self, *, attempt_id: str, job_id: str, result: ResearchResult,
        cached_from_attempt_id: str, timestamp: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO research_provider_attempts(
                attempt_id, job_id, provider_id, adapter_version, query_id,
                query_fingerprint, outcome, cache_hit, cached_from_attempt_id,
                attempt_number, started_at, completed_at, retry_after_seconds, error_category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, NULL, NULL)
            """,
            (
                attempt_id, job_id, result.provider_id, result.adapter_version,
                result.query_id, result.query_fingerprint, result.status.value,
                cached_from_attempt_id, timestamp, timestamp,
            ),
        )
        self.connection.commit()

    def record_attempt(
        self, *, attempt_id: str, job_id: str, canonical_track_id: str,
        subject_fingerprint: str, result: ResearchResult,
        assertion_ids: tuple[str, ...], evidences: tuple[ResearchEvidence, ...],
        started_at: str, attempt_number: int, cache_key: str | None, request_schema_version: str,
        fresh_until: str | None, negative_until: str | None,
    ) -> None:
        if len(assertion_ids) != len(result.assertions):
            raise ValueError("assertion ID count mismatch")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO research_provider_attempts(
                    attempt_id, job_id, provider_id, adapter_version, query_id,
                    query_fingerprint, outcome, cache_hit, cached_from_attempt_id,
                    attempt_number, started_at, completed_at, retry_after_seconds, error_category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, job_id, result.provider_id, result.adapter_version,
                    result.query_id, result.query_fingerprint, result.status.value,
                    attempt_number, started_at, result.retrieved_at, result.retry_after_seconds,
                    result.error_category,
                ),
            )
            for assertion_id, assertion in zip(assertion_ids, result.assertions, strict=True):
                value_json = json.dumps(assertion.asserted_value, sort_keys=True, separators=(",", ":"))
                digest = __import__("hashlib").sha256(
                    f"{result.provider_id}\0{assertion.assertion_key}\0{value_json}".encode()
                ).hexdigest()
                self.connection.execute(
                    """
                    INSERT INTO research_provider_assertions(
                        assertion_id, attempt_id, assertion_key, canonical_track_id,
                        subject_fingerprint, evidence_type, asserted_value_json,
                        evidence_kind, source, source_reference, subject_scope,
                        subject_match, native_confidence, quality_band, observed_at,
                        retrieved_at, content_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assertion_id, attempt_id, assertion.assertion_key, canonical_track_id,
                        subject_fingerprint, assertion.evidence_type.value, value_json,
                        assertion.kind.value, assertion.source.value, assertion.source_reference,
                        assertion.subject_scope.value, assertion.subject_match.value,
                        assertion.native_confidence, assertion.quality_band.value,
                        assertion.observed_at, result.retrieved_at, digest,
                    ),
                )
            for evidence in evidences:
                self.connection.execute(
                    """
                    UPDATE research_evidence
                    SET evidence_state = 'invalidated', invalidated_at = ?,
                        invalidation_reason = 'superseded_provider_assertion'
                    WHERE canonical_track_id = ? AND provider_id = ? AND claim_key = ?
                      AND source_reference = ? AND evidence_state = 'active'
                    """,
                    (
                        result.retrieved_at, evidence.canonical_track_id,
                        evidence.provider_id, evidence.claim_key, evidence.source_reference,
                    ),
                )
                self._insert_evidence(evidence)
            if cache_key is not None:
                self.connection.execute(
                    """
                    INSERT INTO research_cache_entries(
                        cache_key, provider_id, adapter_version, request_schema_version,
                        canonical_track_id, subject_fingerprint, query_fingerprint,
                        outcome, producing_attempt_id, created_at, fresh_until,
                        negative_until, invalidated_at, invalidation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        outcome = excluded.outcome,
                        producing_attempt_id = excluded.producing_attempt_id,
                        created_at = excluded.created_at,
                        fresh_until = excluded.fresh_until,
                        negative_until = excluded.negative_until,
                        invalidated_at = NULL,
                        invalidation_reason = NULL
                    """,
                    (
                        cache_key, result.provider_id, result.adapter_version,
                        request_schema_version, canonical_track_id, subject_fingerprint,
                        result.query_fingerprint, result.status.value, attempt_id,
                        result.retrieved_at, fresh_until, negative_until,
                    ),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _insert_evidence(self, evidence: ResearchEvidence) -> None:
        self.connection.execute(
            """
            INSERT INTO research_evidence(
                evidence_id, canonical_track_id, source, kind, normalized_value,
                source_reference, confidence, created_at, provider_assertion_id,
                provider_id, provider_version, request_id, query_id, subject_fingerprint,
                subject_scope, subject_match, evidence_type, claim_key, quality_band,
                normalized_schema_version, normalizer_version, retrieved_at, fresh_until,
                evidence_state, content_digest, invalidated_at, invalidation_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(evidence_id) DO UPDATE SET
                provider_assertion_id = excluded.provider_assertion_id,
                request_id = excluded.request_id,
                query_id = excluded.query_id,
                retrieved_at = excluded.retrieved_at,
                fresh_until = excluded.fresh_until,
                evidence_state = 'active',
                invalidated_at = NULL,
                invalidation_reason = NULL
            """,
            (
                evidence.evidence_id, evidence.canonical_track_id, evidence.source.value,
                evidence.kind.value, evidence.normalized_value, evidence.source_reference,
                evidence.confidence, utc_now(), evidence.provider_assertion_id,
                evidence.provider_id, evidence.provider_version, evidence.request_id,
                evidence.query_id, evidence.subject_fingerprint, evidence.subject_scope,
                evidence.subject_match, evidence.evidence_type, evidence.claim_key,
                evidence.quality_band, evidence.normalized_schema_version,
                evidence.normalizer_version, evidence.retrieved_at, evidence.fresh_until,
                evidence.evidence_state, evidence.content_digest, evidence.invalidated_at,
                evidence.invalidation_reason,
            ),
        )

    def mark_expired_evidence_stale(self, canonical_track_id: str, now: str) -> None:
        self.connection.execute(
            """
            UPDATE research_evidence SET evidence_state = 'stale'
            WHERE canonical_track_id = ? AND evidence_state = 'active'
              AND fresh_until IS NOT NULL AND fresh_until <= ?
            """,
            (canonical_track_id, now),
        )
        self.connection.commit()

    def evidence_for_track(self, canonical_track_id: str) -> tuple[ResearchEvidence, ...]:
        rows = self.connection.execute(
            "SELECT * FROM research_evidence WHERE canonical_track_id = ? ORDER BY evidence_id",
            (canonical_track_id,),
        ).fetchall()
        return tuple(self._evidence_from_row(row) for row in rows)

    def attempts_for_track(self, canonical_track_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute(
            """
            SELECT a.* FROM research_provider_attempts AS a
            JOIN research_jobs AS j USING (job_id)
            WHERE j.canonical_track_id = ?
            ORDER BY j.requested_at, a.provider_id, a.attempt_number, a.rowid
            """,
            (canonical_track_id,),
        ))

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> ResearchEvidence:
        return ResearchEvidence(
            row["evidence_id"], row["canonical_track_id"], ResearchSource(row["source"]),
            EvidenceKind(row["kind"]), row["normalized_value"], row["source_reference"],
            row["confidence"], row["provider_assertion_id"], row["provider_id"],
            row["provider_version"], row["request_id"], row["query_id"],
            row["subject_fingerprint"], row["subject_scope"], row["subject_match"],
            row["evidence_type"], row["claim_key"], row["quality_band"],
            row["normalized_schema_version"], row["normalizer_version"],
            row["retrieved_at"], row["fresh_until"], row["evidence_state"],
            row["content_digest"], row["invalidated_at"], row["invalidation_reason"],
        )
