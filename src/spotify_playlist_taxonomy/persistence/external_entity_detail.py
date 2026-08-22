"""Generic provider-entity detail observations; never identity or candidate owned."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .identity_external_evidence import normalize_external_entity_id


_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def normalize_associated_isrc(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "", value).upper()
    if not _ISRC.fullmatch(normalized):
        raise ValueError("associated ISRC must be valid")
    return normalized


def detail_content_digest(*, isrcs_available: bool, associated_isrcs: tuple[str, ...], recording_length_ms: int | None) -> str:
    payload = {
        "schema": "external-entity-detail.v1",
        "isrcs_available": isrcs_available,
        "associated_isrcs": list(associated_isrcs) if isrcs_available else [],
        "recording_length_ms": recording_length_ms,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExternalEntityDetailEvidence:
    evidence_id: str
    provider: str
    entity_type: str
    external_id: str
    lookup_state: str
    lookup_method: str
    query_fingerprint: str
    adapter_version: str
    retrieved_at: str
    fresh_until: str | None
    content_digest: str
    isrcs_available: bool
    recording_length_ms: int | None


@dataclass(frozen=True, slots=True)
class ExternalEntityDetailIsrcAssertion:
    evidence_id: str
    normalized_isrc: str


class ExternalEntityDetailStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def upsert(self, item: ExternalEntityDetailEvidence, associated_isrcs: tuple[str, ...] = (), *, now: str | None = None) -> None:
        external_id = normalize_external_entity_id(item.provider, item.entity_type, item.external_id)
        if external_id is None:
            raise ValueError("external entity detail requires an external ID")
        if item.lookup_state not in {"resolved", "not_found", "lookup_failed"}:
            raise ValueError("unsupported detail lookup state")
        if item.recording_length_ms is not None and (isinstance(item.recording_length_ms, bool) or item.recording_length_ms < 0):
            raise ValueError("recording length must be a non-negative integer")
        normalized_isrcs = tuple(sorted({normalize_associated_isrc(value) for value in associated_isrcs}))
        if not item.isrcs_available and normalized_isrcs:
            raise ValueError("unavailable ISRC list cannot contain assertions")
        if item.lookup_state != "resolved" and (item.isrcs_available or item.recording_length_ms is not None):
            raise ValueError("non-resolved detail cannot contain detail assertions")
        expected_digest = detail_content_digest(
            isrcs_available=item.isrcs_available,
            associated_isrcs=normalized_isrcs,
            recording_length_ms=item.recording_length_ms,
        )
        if item.content_digest != expected_digest:
            raise ValueError("detail content digest does not match normalized content")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get(item.provider, item.entity_type, external_id)
            if item.lookup_state == "lookup_failed" and current is not None and _is_fresh(current, now or item.retrieved_at):
                self.connection.rollback()
                return
            self.connection.execute("DELETE FROM external_entity_detail_evidence WHERE provider=? AND entity_type=? AND external_id=?", (item.provider, item.entity_type, external_id))
            self.connection.execute(
                "INSERT INTO external_entity_detail_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.evidence_id, item.provider, item.entity_type, external_id, item.lookup_state,
                 item.lookup_method, item.query_fingerprint, item.adapter_version, item.retrieved_at,
                 item.fresh_until, item.content_digest, int(item.isrcs_available), item.recording_length_ms),
            )
            self.connection.executemany(
                "INSERT INTO external_entity_detail_isrc_assertions VALUES (?,?)",
                ((item.evidence_id, value) for value in normalized_isrcs),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def get(self, provider: str, entity_type: str, external_id: str) -> ExternalEntityDetailEvidence | None:
        normalized = normalize_external_entity_id(provider, entity_type, external_id)
        row = self.connection.execute(
            "SELECT * FROM external_entity_detail_evidence WHERE provider=? AND entity_type=? AND external_id=?",
            (provider, entity_type, normalized),
        ).fetchone()
        return None if row is None else self._evidence(row)

    def for_entities(self, provider: str, entity_type: str, external_ids: tuple[str, ...]) -> tuple[ExternalEntityDetailEvidence, ...]:
        values = tuple(sorted({normalize_external_entity_id(provider, entity_type, item) for item in external_ids}))
        if not values:
            return ()
        marks = ",".join("?" for _ in values)
        return tuple(self._evidence(row) for row in self.connection.execute(
            f"SELECT * FROM external_entity_detail_evidence WHERE provider=? AND entity_type=? AND external_id IN ({marks}) ORDER BY external_id",
            (provider, entity_type, *values),
        ))

    def isrc_assertions(self, evidence_id: str) -> tuple[ExternalEntityDetailIsrcAssertion, ...]:
        return tuple(ExternalEntityDetailIsrcAssertion(*row) for row in self.connection.execute(
            "SELECT evidence_id, normalized_isrc FROM external_entity_detail_isrc_assertions WHERE evidence_id=? ORDER BY normalized_isrc",
            (evidence_id,),
        ))

    @staticmethod
    def _evidence(row: sqlite3.Row) -> ExternalEntityDetailEvidence:
        return ExternalEntityDetailEvidence(*row[:11], bool(row[11]), row[12])


def _is_fresh(item: ExternalEntityDetailEvidence, now: str) -> bool:
    if item.lookup_state != "resolved" or item.fresh_until is None:
        return False
    try:
        return datetime.fromisoformat(item.fresh_until.replace("Z", "+00:00")).astimezone(UTC) > datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return False
