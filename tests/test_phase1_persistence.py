from pathlib import Path
import sqlite3
import tempfile
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.config import TaxonomyDocument, load_taxonomy_document
from spotify_playlist_taxonomy.domain.models import (
    AssignmentDecision, CalibrationExample, CalibrationPolarity, CanonicalTrack,
    CanonicalTrackIdentityLink, CharacteristicOrigin, ClassificationProposal,
    DecisionOrigin, DerivedCharacteristic, EvidenceKind, IdentityProvider, MatchState,
    PlatformPlaylistRegistration, PlatformTrackIdentity, PlaylistRole, ProposalOrigin,
    ResearchEvidence, ResearchSource, ReviewStatus, TrackMatchAssessment, UserDecision,
)
from spotify_playlist_taxonomy.persistence import (
    MigrationError, RecordConflictError, SQLiteStateStore, TaxonomySyncError,
    apply_migrations, connect_database, current_schema_version, sync_taxonomy, taxonomy_state,
)

ROOT = Path(__file__).resolve().parents[1]


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "app.db"
        self.connection = connect_database(self.database_path)
        apply_migrations(self.connection)
        self.taxonomy = load_taxonomy_document(ROOT / "config" / "taxonomy.yaml")
        sync_taxonomy(self.connection, self.taxonomy)
        self.store = SQLiteStateStore(self.connection)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_migrations_and_taxonomy_sync_are_idempotent(self) -> None:
        self.assertEqual(4, current_schema_version(self.connection))
        self.assertEqual(4, apply_migrations(self.connection))
        self.assertFalse(sync_taxonomy(self.connection, self.taxonomy))
        state = taxonomy_state(self.connection, self.taxonomy)
        self.assertTrue(state.in_sync)
        self.assertEqual(36, state.persisted_playlist_count)

    def test_same_version_changed_content_is_rejected(self) -> None:
        payload = self.taxonomy.model_dump(mode="json")
        payload["playlists"][0]["display_name"] = "Changed without version"
        changed = TaxonomyDocument.model_validate(payload)
        with self.assertRaisesRegex(TaxonomySyncError, "without a taxonomy_version change"):
            sync_taxonomy(self.connection, changed)

    def test_status_detects_persisted_taxonomy_row_drift(self) -> None:
        self.connection.execute(
            "UPDATE logical_playlist_revisions SET display_name = ? "
            "WHERE taxonomy_version = ? AND logical_id = ?",
            ("Tampered synthetic value", "taxonomy.v1", "genre.pop"),
        )
        self.connection.commit()
        self.assertFalse(taxonomy_state(self.connection, self.taxonomy).in_sync)
        with self.assertRaisesRegex(TaxonomySyncError, "do not match"):
            sync_taxonomy(self.connection, self.taxonomy)

    def test_new_version_cannot_remove_or_change_dimension(self) -> None:
        missing_payload = self.taxonomy.model_dump(mode="json")
        missing_payload["taxonomy_version"] = "taxonomy.v2"
        missing_payload["playlists"] = missing_payload["playlists"][1:]
        with self.assertRaisesRegex(TaxonomySyncError, "omits existing logical IDs"):
            sync_taxonomy(self.connection, TaxonomyDocument.model_validate(missing_payload))

        changed_payload = self.taxonomy.model_dump(mode="json")
        changed_payload["taxonomy_version"] = "taxonomy.v2"
        changed_payload["playlists"][0]["logical_id"] = "vibe.pop"
        changed_payload["playlists"][0]["dimension"] = "vibe"
        with self.assertRaises(TaxonomySyncError):
            sync_taxonomy(self.connection, TaxonomyDocument.model_validate(changed_payload))

    def test_no_future_workflow_tables_exist(self) -> None:
        tables = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        self.assertTrue({"canonical_tracks", "research_evidence", "classification_proposals"} <= tables)
        self.assertTrue({"research_jobs", "classification_batches", "sync_plans", "operation_journal"}.isdisjoint(tables))

    def test_catalogue_and_matching_round_trip_without_normalized_uniqueness(self) -> None:
        first = CanonicalTrack("track.synthetic.one", "Neon Orbit", ("Invented Signals",))
        second = CanonicalTrack("track.synthetic.two", "Neon Orbit", ("Invented Signals",))
        self.store.save_canonical_track(first)
        self.store.save_canonical_track(second)
        self.assertEqual(first, self.store.get_canonical_track(first.canonical_track_id))
        self.assertEqual(second, self.store.get_canonical_track(second.canonical_track_id))

        left = PlatformTrackIdentity(
            "identity.synthetic.one", IdentityProvider.SPOTIFY, "Neon Orbit",
            ("Invented Signals",), "spotify.synthetic.one", "Studio",
        )
        right = PlatformTrackIdentity(
            "identity.synthetic.two", IdentityProvider.USER_IMPORT, "Neon Orbit",
            ("Invented Signals",), version_label="Live",
        )
        self.store.save_track_identity(left)
        self.store.save_track_identity(right)
        self.assertEqual(left, self.store.get_track_identity(left.identity_id))
        link = CanonicalTrackIdentityLink("link.synthetic.one", first.canonical_track_id, left.identity_id)
        self.store.save_identity_link(link)
        self.assertEqual(link, self.store.get_identity_link(link.link_id))
        assessment = TrackMatchAssessment(
            left.identity_id, right.identity_id, MatchState.USER_CONFIRMATION_REQUIRED,
            0.25, ("normalized_title_artist_match", "conflicting_version_labels"),
        )
        self.store.save_match_assessment(assessment)
        self.assertEqual(assessment, self.store.get_latest_match_assessment(left.identity_id, right.identity_id))

    def test_playlist_and_classification_state_round_trip(self) -> None:
        track = CanonicalTrack("track.synthetic.classified", "Paper Satellites", ("Example Avenue",))
        self.store.save_canonical_track(track)
        registration = PlatformPlaylistRegistration(
            "registration.synthetic.source", IdentityProvider.SPOTIFY, "playlist.synthetic.source",
            PlaylistRole.SOURCE, display_name="Synthetic Source",
        )
        self.store.save_playlist_registration(registration)
        self.assertEqual(registration, self.store.get_playlist_registration(registration.registration_id))

        evidence = ResearchEvidence(
            "evidence.synthetic.one", track.canonical_track_id, ResearchSource.PUBLICATION,
            EvidenceKind.EDITORIAL_DESCRIPTION, "synthetic energetic description",
            "https://example.invalid/synthetic", 0.8,
        )
        self.store.save_evidence(evidence)
        self.assertEqual(evidence, self.store.get_evidence(evidence.evidence_id))
        characteristic = DerivedCharacteristic(
            "characteristic.synthetic.one", track.canonical_track_id, "energy", "high",
            CharacteristicOrigin.CODEX_INTERPRETATION, (evidence.evidence_id,), 0.8,
            "derive.synthetic.v1", "Synthetic evidence indicates high energy.",
        )
        self.store.save_characteristic(characteristic)
        self.assertEqual(characteristic, self.store.get_characteristic(characteristic.characteristic_id))
        self.store.save_specification_version(
            specification_version="spec.synthetic.v1", schema_version="1",
            logical_playlist_id="vibe.hype", digest="a" * 64, status="draft",
        )
        proposal = ClassificationProposal(
            "proposal.synthetic.one", track.canonical_track_id, "vibe.hype",
            AssignmentDecision.INCLUDE, 0.82, ProposalOrigin.CODEX_CLASSIFIER,
            (evidence.evidence_id,), (characteristic.characteristic_id,),
            "Synthetic high-energy evidence supports the proposal.",
            "taxonomy.v1", "spec.synthetic.v1", ReviewStatus.NEEDS_REVIEW,
        )
        self.store.save_proposal(proposal)
        self.assertEqual(proposal, self.store.get_proposal(proposal.proposal_id))
        decision = UserDecision(
            "decision.synthetic.one", track.canonical_track_id, "vibe.hype",
            AssignmentDecision.EXCLUDE, DecisionOrigin.USER_OVERRIDE, 1,
        )
        self.store.save_user_decision(decision)
        self.assertEqual(decision, self.store.get_user_decision(decision.decision_id))
        example = CalibrationExample(
            "calibration.synthetic.one", decision.decision_id, "vibe.hype",
            CalibrationPolarity.NEGATIVE,
        )
        self.store.save_calibration_example(example)
        self.assertEqual(example, self.store.get_calibration_example(example.calibration_example_id))

    def test_foreign_keys_and_conflicts_are_enforced(self) -> None:
        with self.assertRaises(RecordConflictError):
            self.store.save_identity_link(CanonicalTrackIdentityLink(
                "link.synthetic.missing", "track.missing", "identity.missing"
            ))

    def test_newer_schema_is_rejected(self) -> None:
        self.connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (99, 'future.sql', 'x', 'now')"
        )
        self.connection.commit()
        with self.assertRaisesRegex(MigrationError, "newer"):
            apply_migrations(self.connection)


if __name__ == "__main__":
    unittest.main()
