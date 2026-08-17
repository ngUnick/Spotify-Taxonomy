"""Supervised, offline Phase 8 batch export and transactional result import."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.persistence.database import utc_now
from spotify_playlist_taxonomy.workflows.phase8_inbound import (
    InboundResultValidationError,
    OutboundBatchContext,
    parse_inbound_result,
    validate_inbound_result,
)


SCHEMA_VERSION = "codex-batch.v1"


class Phase8ImportError(ValueError):
    """A compact, fail-closed import error that never includes inbound payload content."""


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    batch_id: str
    item_count: int
    proposal_count: int
    state: str
    idempotent: bool


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class Phase8Service:
    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle, outbound: Path) -> None:
        self.connection = connection
        self.bundle = bundle
        self.outbound = outbound

    def create(self, canonical_ids: tuple[str, ...]) -> tuple[str, Path]:
        if not canonical_ids or len(canonical_ids) > 25 or len(set(canonical_ids)) != len(canonical_ids):
            raise ValueError("batch requires 1-25 distinct explicit canonical IDs")
        items: list[dict[str, Any]] = []
        for index, canonical_id in enumerate(canonical_ids):
            if not self.connection.execute(
                "SELECT 1 FROM canonical_tracks WHERE canonical_track_id=?", (canonical_id,)
            ).fetchone():
                raise ValueError("unknown canonical ID")
            evidence = [
                {"evidence_id": row["evidence_id"], "evidence_type": row["evidence_type"], "value": row["normalized_value"]}
                for row in self.connection.execute(
                    "SELECT evidence_id, normalized_value, evidence_type FROM research_evidence "
                    "WHERE canonical_track_id=? AND evidence_state='active'",
                    (canonical_id,),
                )
            ]
            characteristics = [
                {"characteristic_id": row["derivation_id"], "name": row["characteristic_name"], "value": row["value_json"]}
                for row in self.connection.execute(
                    "SELECT derivation_id, characteristic_name, value_json FROM phase6_characteristic_derivations "
                    "WHERE canonical_track_id=? AND state='current'",
                    (canonical_id,),
                )
            ]
            proposals = [
                {
                    "logical_playlist_id": row["logical_playlist_id"],
                    "decision": row["decision"],
                    "state": row["proposal_state"],
                    "explanation": json.loads(row["explanation_json"]),
                }
                for row in self.connection.execute(
                    "SELECT logical_playlist_id, decision, proposal_state, explanation_json "
                    "FROM phase6_proposals WHERE canonical_track_id=?",
                    (canonical_id,),
                )
            ]
            items.append(
                {
                    "item_id": f"item.{index + 1:03d}",
                    "canonical_ref": canonical_id,
                    "evidence": evidence,
                    "characteristics": characteristics,
                    "deterministic_proposals": proposals,
                }
            )
        document = {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": self.bundle.taxonomy.taxonomy_version,
            "bundle_digest": self.bundle.digest,
            "items": items,
            "specifications": [item.model_dump(mode="json") for item in self.bundle.specifications],
        }
        outbound_digest = digest(document)
        existing = self.connection.execute(
            "SELECT batch_id, outbound_path FROM codex_batches WHERE outbound_digest=?", (outbound_digest,)
        ).fetchone()
        if existing is not None:
            return existing["batch_id"], Path(existing["outbound_path"])

        batch_id = f"batch.{uuid.uuid4().hex}"
        document["batch_id"] = batch_id
        self.outbound.mkdir(parents=True, exist_ok=True)
        path = self.outbound / f"{batch_id}.json"
        temporary_path = path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary_path, path)
        self.connection.execute(
            "INSERT INTO codex_batches VALUES (?, ?, ?, ?, 'awaiting_result', ?, ?, ?, ?)",
            (
                batch_id,
                SCHEMA_VERSION,
                self.bundle.taxonomy.taxonomy_version,
                self.bundle.digest,
                outbound_digest,
                len(items),
                str(path),
                utc_now(),
            ),
        )
        self.connection.executemany(
            "INSERT INTO codex_batch_items VALUES (?, ?, ?, ?)",
            [
                (batch_id, item["item_id"], item["canonical_ref"], json.dumps(item, separators=(",", ":")))
                for item in items
            ],
        )
        self.connection.commit()
        return batch_id, path

    def import_result(self, result_file: Path) -> ImportOutcome:
        """Validate and atomically import a local supervised result file."""
        try:
            raw = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise Phase8ImportError("result file is unreadable or invalid JSON") from None
        try:
            result = parse_inbound_result(raw)
        except InboundResultValidationError as error:
            raise Phase8ImportError(str(error)) from None

        batch = self.connection.execute(
            "SELECT * FROM codex_batches WHERE batch_id=?", (result.batch_id,)
        ).fetchone()
        if batch is None:
            raise Phase8ImportError("unknown outbound batch")
        if batch["state"] in {"stale", "superseded"}:
            raise Phase8ImportError("outbound batch is not importable")
        if batch["state"] == "awaiting_result" and (
            batch["taxonomy_version"] != self.bundle.taxonomy.taxonomy_version
            or batch["bundle_digest"] != self.bundle.digest
        ):
            self.connection.execute("UPDATE codex_batches SET state='stale' WHERE batch_id=?", (result.batch_id,))
            self.connection.commit()
            raise Phase8ImportError("outbound batch is stale")
        try:
            outbound_document = json.loads(Path(batch["outbound_path"]).read_text(encoding="utf-8"))
            context = OutboundBatchContext.from_phase8a_export(
                batch_metadata=batch,
                outbound_document=outbound_document,
                output_target_ids=frozenset(item.output_target_id for item in self.bundle.output_targets.targets),
            )
            validate_inbound_result(result, context)
        except (OSError, UnicodeError, json.JSONDecodeError, InboundResultValidationError) as error:
            raise Phase8ImportError(str(error) if isinstance(error, InboundResultValidationError) else "outbound batch context is unavailable") from None

        result_digest = digest(result.model_dump(mode="json"))
        prior = self.connection.execute(
            "SELECT * FROM codex_batch_imports WHERE batch_id=?", (result.batch_id,)
        ).fetchone()
        if prior is not None:
            if prior["result_digest"] != result_digest:
                raise Phase8ImportError("conflicting replay for an imported batch")
            return ImportOutcome(result.batch_id, prior["item_count"], prior["proposal_count"], "imported", True)

        proposal_count = sum(len(item.classifications) for item in result.items)
        import_id = f"codex-import.{uuid.uuid4().hex}"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO codex_batch_imports VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    import_id,
                    result.batch_id,
                    result.result_schema_version,
                    result_digest,
                    len(result.items),
                    proposal_count,
                    utc_now(),
                ),
            )
            canonical_ids = {
                row["item_id"]: row["canonical_track_id"]
                for row in self.connection.execute(
                    "SELECT item_id, canonical_track_id FROM codex_batch_items WHERE batch_id=?",
                    (result.batch_id,),
                )
            }
            for item in result.items:
                for classification in item.classifications:
                    proposal_id = f"codex-proposal.{uuid.uuid4().hex}"
                    self.connection.execute(
                        """
                        INSERT INTO phase8_codex_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            proposal_id,
                            import_id,
                            result.batch_id,
                            item.item_id,
                            canonical_ids[item.item_id],
                            classification.semantic_specification_id,
                            classification.decision.value,
                            result.taxonomy_version,
                            context.specification_versions[classification.semantic_specification_id],
                            result.specification_bundle_digest,
                            result_digest,
                            classification.justification_summary,
                            utc_now(),
                        ),
                    )
                    self.connection.executemany(
                        "INSERT INTO phase8_codex_proposal_evidence VALUES (?, ?)",
                        [(proposal_id, evidence_id) for evidence_id in classification.evidence_ids],
                    )
                    self.connection.executemany(
                        "INSERT INTO phase8_codex_proposal_characteristics VALUES (?, ?)",
                        [(proposal_id, characteristic_id) for characteristic_id in classification.characteristic_ids],
                    )
            lifecycle = self.connection.execute(
                "UPDATE codex_batches SET state='imported' WHERE batch_id=? AND state='awaiting_result'",
                (result.batch_id,),
            )
            if lifecycle.rowcount != 1:
                raise sqlite3.IntegrityError("batch lifecycle transition failed")
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise Phase8ImportError("result import could not be completed") from error
        return ImportOutcome(result.batch_id, len(result.items), proposal_count, "imported", False)

    def list(self, limit: int = 25) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                """
                SELECT b.*, i.imported_at, i.proposal_count
                FROM codex_batches b LEFT JOIN codex_batch_imports i USING (batch_id)
                ORDER BY b.created_at DESC LIMIT ?
                """,
                (limit,),
            )
        )

    def show(self, batch_id: str) -> tuple[sqlite3.Row, tuple[sqlite3.Row, ...]]:
        row = self.connection.execute(
            """
            SELECT b.*, i.imported_at, i.proposal_count
            FROM codex_batches b LEFT JOIN codex_batch_imports i USING (batch_id)
            WHERE b.batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        if row is None:
            raise LookupError("unknown batch")
        items = tuple(
            self.connection.execute(
                "SELECT item_id, canonical_track_id FROM codex_batch_items WHERE batch_id=? ORDER BY item_id",
                (batch_id,),
            )
        )
        return row, items
