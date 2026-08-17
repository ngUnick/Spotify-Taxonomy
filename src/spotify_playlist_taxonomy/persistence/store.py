"""Basic SQLite round-trip persistence for established Phase 0 domain concepts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from spotify_playlist_taxonomy.domain.models import (
    AssignmentDecision,
    CalibrationExample,
    CalibrationPolarity,
    CanonicalTrack,
    CanonicalTrackIdentityLink,
    CharacteristicOrigin,
    ClassificationProposal,
    DecisionOrigin,
    DerivedCharacteristic,
    EvidenceKind,
    IdentityProvider,
    MatchState,
    PlatformPlaylistRegistration,
    PlatformTrackIdentity,
    PlaylistRole,
    ProposalOrigin,
    ResearchEvidence,
    ResearchSource,
    ReviewStatus,
    TrackMatchAssessment,
    UserDecision,
)
from spotify_playlist_taxonomy.domain.policies import normalize_identity_text

from .database import connect_database, utc_now


class RecordNotFoundError(LookupError):
    pass


class RecordConflictError(RuntimeError):
    pass


class SQLiteStateStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "SQLiteStateStore":
        return cls(connect_database(path))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteStateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            raise RecordConflictError(str(error)) from error
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _required(row: sqlite3.Row | None, record_id: str) -> sqlite3.Row:
        if row is None:
            raise RecordNotFoundError(record_id)
        return row

    def save_canonical_track(self, track: CanonicalTrack) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO canonical_tracks(canonical_track_id, display_title, created_at) VALUES (?, ?, ?)",
                (track.canonical_track_id, track.display_title, utc_now()),
            )
            connection.executemany(
                "INSERT INTO canonical_track_artists(canonical_track_id, position, artist_name) VALUES (?, ?, ?)",
                [(track.canonical_track_id, index, artist) for index, artist in enumerate(track.display_artists)],
            )

    def get_canonical_track(self, canonical_track_id: str) -> CanonicalTrack:
        row = self._required(
            self.connection.execute(
                "SELECT canonical_track_id, display_title FROM canonical_tracks WHERE canonical_track_id = ?",
                (canonical_track_id,),
            ).fetchone(), canonical_track_id,
        )
        artists = tuple(
            item["artist_name"] for item in self.connection.execute(
                "SELECT artist_name FROM canonical_track_artists WHERE canonical_track_id = ? ORDER BY position",
                (canonical_track_id,),
            )
        )
        return CanonicalTrack(row["canonical_track_id"], row["display_title"], artists)

    def save_track_identity(self, identity: PlatformTrackIdentity) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO track_identities(
                    identity_id, provider, provider_track_id, title, normalized_title,
                    version_label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.identity_id, identity.provider.value, identity.provider_track_id,
                    identity.title, normalize_identity_text(identity.title), identity.version_label, utc_now(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO track_identity_artists(
                    identity_id, position, artist_name, normalized_artist_name
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (identity.identity_id, index, artist, normalize_identity_text(artist))
                    for index, artist in enumerate(identity.artists)
                ],
            )

    def get_track_identity(self, identity_id: str) -> PlatformTrackIdentity:
        row = self._required(
            self.connection.execute("SELECT * FROM track_identities WHERE identity_id = ?", (identity_id,)).fetchone(),
            identity_id,
        )
        artists = tuple(
            item["artist_name"] for item in self.connection.execute(
                "SELECT artist_name FROM track_identity_artists WHERE identity_id = ? ORDER BY position",
                (identity_id,),
            )
        )
        return PlatformTrackIdentity(
            row["identity_id"], IdentityProvider(row["provider"]), row["title"], artists,
            row["provider_track_id"], row["version_label"],
        )

    def save_identity_link(self, link: CanonicalTrackIdentityLink) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO canonical_track_identity_links(link_id, canonical_track_id, identity_id, linked_at) "
                "VALUES (?, ?, ?, ?)",
                (link.link_id, link.canonical_track_id, link.identity_id, utc_now()),
            )

    def get_identity_link(self, link_id: str) -> CanonicalTrackIdentityLink:
        row = self._required(
            self.connection.execute(
                "SELECT link_id, canonical_track_id, identity_id FROM canonical_track_identity_links WHERE link_id = ?",
                (link_id,),
            ).fetchone(), link_id,
        )
        return CanonicalTrackIdentityLink(row["link_id"], row["canonical_track_id"], row["identity_id"])

    def save_match_assessment(self, assessment: TrackMatchAssessment) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO track_match_assessments(
                    left_identity_id, right_identity_id, state, confidence, basis_json, assessed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.left_identity_id, assessment.right_identity_id, assessment.state.value,
                    assessment.confidence, json.dumps(assessment.basis, separators=(",", ":")), utc_now(),
                ),
            )

    def get_latest_match_assessment(self, left_id: str, right_id: str) -> TrackMatchAssessment:
        row = self._required(
            self.connection.execute(
                """
                SELECT * FROM track_match_assessments
                WHERE left_identity_id = ? AND right_identity_id = ?
                ORDER BY assessment_id DESC LIMIT 1
                """, (left_id, right_id),
            ).fetchone(), f"{left_id}:{right_id}",
        )
        return TrackMatchAssessment(
            row["left_identity_id"], row["right_identity_id"], MatchState(row["state"]),
            row["confidence"], tuple(json.loads(row["basis_json"])),
        )

    def save_playlist_registration(self, registration: PlatformPlaylistRegistration) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO platform_playlist_registrations(
                    registration_id, provider, provider_playlist_id, role, logical_playlist_id,
                    owner_account_id, display_name, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    registration.registration_id, registration.provider.value,
                    registration.provider_playlist_id, registration.role.value,
                    registration.logical_playlist_id, registration.owner_account_id,
                    registration.display_name, utc_now(),
                ),
            )

    def get_playlist_registration(self, registration_id: str) -> PlatformPlaylistRegistration:
        row = self._required(
            self.connection.execute(
                "SELECT * FROM platform_playlist_registrations WHERE registration_id = ?", (registration_id,)
            ).fetchone(), registration_id,
        )
        return PlatformPlaylistRegistration(
            row["registration_id"], IdentityProvider(row["provider"]), row["provider_playlist_id"],
            PlaylistRole(row["role"]), row["logical_playlist_id"], row["owner_account_id"], row["display_name"],
        )

    def save_specification_version(
        self, *, specification_version: str, schema_version: str,
        logical_playlist_id: str, digest: str, status: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO specification_versions(
                    specification_version, schema_version, logical_playlist_id, digest, status, persisted_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (specification_version, schema_version, logical_playlist_id, digest, status, utc_now()),
            )

    def save_evidence(self, evidence: ResearchEvidence) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO research_evidence(
                    evidence_id, canonical_track_id, source, kind, normalized_value,
                    source_reference, confidence, created_at, provider_assertion_id,
                    provider_id, provider_version, request_id, query_id, subject_fingerprint,
                    subject_scope, subject_match, evidence_type, claim_key, quality_band,
                    normalized_schema_version, normalizer_version, retrieved_at, fresh_until,
                    evidence_state, content_digest, invalidated_at, invalidation_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def get_evidence(self, evidence_id: str) -> ResearchEvidence:
        row = self._required(
            self.connection.execute("SELECT * FROM research_evidence WHERE evidence_id = ?", (evidence_id,)).fetchone(),
            evidence_id,
        )
        return ResearchEvidence(
            row["evidence_id"], row["canonical_track_id"], ResearchSource(row["source"]),
            EvidenceKind(row["kind"]), row["normalized_value"], row["source_reference"], row["confidence"],
            row["provider_assertion_id"], row["provider_id"], row["provider_version"],
            row["request_id"], row["query_id"], row["subject_fingerprint"],
            row["subject_scope"], row["subject_match"], row["evidence_type"],
            row["claim_key"], row["quality_band"], row["normalized_schema_version"],
            row["normalizer_version"], row["retrieved_at"], row["fresh_until"],
            row["evidence_state"], row["content_digest"], row["invalidated_at"],
            row["invalidation_reason"],
        )

    def save_characteristic(self, item: DerivedCharacteristic) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO derived_characteristics(
                    characteristic_id, canonical_track_id, name, value, origin, confidence,
                    derivation_version, justification_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.characteristic_id, item.canonical_track_id, item.name, item.value,
                    item.origin.value, item.confidence, item.derivation_version,
                    item.justification_summary, utc_now(),
                ),
            )
            connection.executemany(
                "INSERT INTO characteristic_evidence(characteristic_id, evidence_id) VALUES (?, ?)",
                [(item.characteristic_id, evidence_id) for evidence_id in item.evidence_ids],
            )

    def get_characteristic(self, characteristic_id: str) -> DerivedCharacteristic:
        row = self._required(
            self.connection.execute(
                "SELECT * FROM derived_characteristics WHERE characteristic_id = ?", (characteristic_id,)
            ).fetchone(), characteristic_id,
        )
        evidence_ids = tuple(
            item["evidence_id"] for item in self.connection.execute(
                "SELECT evidence_id FROM characteristic_evidence WHERE characteristic_id = ? ORDER BY evidence_id",
                (characteristic_id,),
            )
        )
        return DerivedCharacteristic(
            row["characteristic_id"], row["canonical_track_id"], row["name"], row["value"],
            CharacteristicOrigin(row["origin"]), evidence_ids, row["confidence"],
            row["derivation_version"], row["justification_summary"],
        )

    def save_proposal(self, item: ClassificationProposal) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO classification_proposals(
                    proposal_id, canonical_track_id, logical_playlist_id, decision, confidence,
                    origin, justification_summary, taxonomy_version, specification_version,
                    review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.proposal_id, item.canonical_track_id, item.logical_playlist_id,
                    item.decision.value, item.confidence, item.origin.value, item.justification_summary,
                    item.taxonomy_version, item.specification_version, item.review_status.value, utc_now(),
                ),
            )
            connection.executemany(
                "INSERT INTO proposal_evidence(proposal_id, evidence_id) VALUES (?, ?)",
                [(item.proposal_id, value) for value in item.evidence_ids],
            )
            connection.executemany(
                "INSERT INTO proposal_characteristics(proposal_id, characteristic_id) VALUES (?, ?)",
                [(item.proposal_id, value) for value in item.characteristic_ids],
            )

    def get_proposal(self, proposal_id: str) -> ClassificationProposal:
        row = self._required(
            self.connection.execute("SELECT * FROM classification_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone(),
            proposal_id,
        )
        evidence = tuple(item[0] for item in self.connection.execute(
            "SELECT evidence_id FROM proposal_evidence WHERE proposal_id = ? ORDER BY evidence_id", (proposal_id,)
        ))
        characteristics = tuple(item[0] for item in self.connection.execute(
            "SELECT characteristic_id FROM proposal_characteristics WHERE proposal_id = ? ORDER BY characteristic_id",
            (proposal_id,),
        ))
        return ClassificationProposal(
            row["proposal_id"], row["canonical_track_id"], row["logical_playlist_id"],
            AssignmentDecision(row["decision"]), row["confidence"], ProposalOrigin(row["origin"]),
            evidence, characteristics, row["justification_summary"], row["taxonomy_version"],
            row["specification_version"], ReviewStatus(row["review_status"]),
        )

    def save_user_decision(self, item: UserDecision) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO user_decisions(
                    decision_id, canonical_track_id, logical_playlist_id, decision, origin, sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.decision_id, item.canonical_track_id, item.logical_playlist_id,
                    item.decision.value, item.origin.value, item.sequence, utc_now(),
                ),
            )

    def get_user_decision(self, decision_id: str) -> UserDecision:
        row = self._required(
            self.connection.execute("SELECT * FROM user_decisions WHERE decision_id = ?", (decision_id,)).fetchone(),
            decision_id,
        )
        return UserDecision(
            row["decision_id"], row["canonical_track_id"], row["logical_playlist_id"],
            AssignmentDecision(row["decision"]), DecisionOrigin(row["origin"]), row["sequence"],
        )

    def save_calibration_example(self, item: CalibrationExample) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO calibration_examples(
                    calibration_example_id, user_decision_id, logical_playlist_id,
                    polarity, selected, selected_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.calibration_example_id, item.user_decision_id, item.logical_playlist_id,
                    item.polarity.value, int(item.selected), utc_now(),
                ),
            )

    def get_calibration_example(self, example_id: str) -> CalibrationExample:
        row = self._required(
            self.connection.execute(
                "SELECT * FROM calibration_examples WHERE calibration_example_id = ?", (example_id,)
            ).fetchone(), example_id,
        )
        return CalibrationExample(
            row["calibration_example_id"], row["user_decision_id"], row["logical_playlist_id"],
            CalibrationPolarity(row["polarity"]), bool(row["selected"]),
        )
