from __future__ import annotations
import tempfile
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner
from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.adapters.spotify.reader import SpotifyRecordingEvidence
from spotify_playlist_taxonomy.adapters.research.musicbrainz import MusicBrainzRecordingLookupResult
from spotify_playlist_taxonomy.domain.ingestion import StagedSourceEntry
from spotify_playlist_taxonomy.domain.models import IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database
from spotify_playlist_taxonomy.persistence.database import load_migrations
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.persistence.identity_external_evidence import IdentityExternalEntityEvidence, IdentityExternalEvidenceStore
from spotify_playlist_taxonomy.persistence.external_entity_detail import (
    ExternalEntityDetailEvidence, ExternalEntityDetailStore, detail_content_digest,
)
from spotify_playlist_taxonomy.workflows.match_evidence import MatchEvidenceService
from spotify_playlist_taxonomy.workflows.match_evidence_backfill import MatchEvidenceBackfillService
from spotify_playlist_taxonomy.workflows.match_evidence_review import MatchEvidenceReviewService
from spotify_playlist_taxonomy.workflows.deterministic_match import TierAEligibilityService, evaluate_comparison_consistency
from spotify_playlist_taxonomy.workflows.external_recording_targets import EXTERNAL_RECORDING_CANDIDATE_STRATUM, ExternalRecordingCandidateFetchService, ExternalRecordingComparisonMember, ExternalRecordingComparisonService, ExternalRecordingDetailTargetProjectionService, ExternalRecordingEvidenceFetchService, ExternalRecordingTargetProjectionService, _external_recording_relationship

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"


class EvidenceReader:
    def __init__(self, responses: dict[str, SpotifyRecordingEvidence | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def read_track_recording_evidence(self, provider_track_id: str) -> SpotifyRecordingEvidence:
        self.calls.append(provider_track_id)
        response = self.responses[provider_track_id]
        if isinstance(response, Exception):
            raise response
        return response

    def read_current_account(self):
        return type("Account", (), {"account_id": "account.synthetic"})()


class MatchEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        self.store = Phase3Store(self.connection)
        self.binding = self.store.bind_account("binding.synthetic", "account.synthetic", "user.synthetic")
        self.source = self.store.register_liked_source("source.synthetic", self.binding.binding_id)
        identities = (
            PlatformTrackIdentity("identity.unresolved", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), "provider.unresolved", "live", duration_ms=100000, isrc="us-rc1-76-07839"),
            PlatformTrackIdentity("identity.linked.a", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), "provider.linked.a", "live", duration_ms=100000, isrc="USRC17607839"),
            PlatformTrackIdentity("identity.linked.b", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), "provider.linked.b", "studio", duration_ms=106001, isrc="GBAYE6800011"),
        )
        self.store.start_scan(scan_id="scan.synthetic", source_id=self.source.source_id, binding_id=self.binding.binding_id, starting_snapshot_id=None, starting_total=3, initial_page_signature=None, digest_version="synthetic.v1")
        self.store.stage_page(scan_id="scan.synthetic", binding_id=self.binding.binding_id, entries=tuple(StagedSourceEntry(index, 0, item) for index, item in enumerate(identities)), next_cursor=None, next_offset=3)
        self.store.publish_scan("scan.synthetic", self.binding.binding_id, None)
        self.connection.execute("INSERT INTO canonical_tracks VALUES ('canonical.synthetic', 'Synthetic', 'now')")
        self.connection.execute("INSERT INTO canonical_track_artists VALUES ('canonical.synthetic', 0, 'Artist')")
        for suffix in ("a", "b"):
            self.connection.execute("INSERT INTO account_canonical_identity_links VALUES (?, ?, ?, ?, 'now')", (f"link.{suffix}", self.binding.binding_id, f"identity.linked.{suffix}", "canonical.synthetic"))
        self.connection.execute("""INSERT INTO canonical_match_candidates(candidate_id, account_binding_id, identity_id, canonical_track_id, action, state, confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at) VALUES ('candidate.synthetic', ?, 'identity.unresolved', 'canonical.synthetic', 'link_existing', 'ambiguous', .5, '[]', 'synthetic.v1', 'fingerprint', 'key', 'now')""", (self.binding.binding_id,))
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close(); self.temp.cleanup()

    def test_schema_eighteen_migrates_schema_seventeen_and_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "older.db")
            migrations = load_migrations()
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for migration in migrations[:17]:
                connection.executescript(migration.sql)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, 'now')", (migration.version, migration.name, migration.checksum))
            connection.commit()
            self.assertEqual(18, apply_migrations(connection))
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("identity_external_entity_evidence", tables)
            self.assertIn("external_entity_detail_evidence", tables)
            self.assertIn("external_entity_detail_isrc_assertions", tables)
            self.assertIn("deterministic_match_decisions", tables)
            connection.close()
        self.assertEqual(18, apply_migrations(self.connection))
        self.assertIn(
            "identity_external_entity_evidence",
            {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")},
        )

    def test_analysis_is_read_only_and_counts_pairs_without_selecting_one(self) -> None:
        before = self.connection.total_changes
        report = dict(MatchEvidenceService(self.store, self.binding.binding_id).report().metrics)
        self.assertEqual(before, self.connection.total_changes)
        self.assertEqual(1, report["unresolved_identity_items"])
        self.assertEqual(1, report["multiple_comparison_identities"])
        self.assertEqual(2, report["comparison_pairs_with_duration"])
        self.assertEqual(1, report["delta_exact"])
        self.assertEqual(1, report["delta_gt_5s"])
        self.assertEqual(1, report["same_isrc"])
        self.assertEqual(1, report["different_isrc"])
        self.assertEqual(1, report["version_explicit_conflict"])
        self.assertEqual(0, report["same_isrc_version_conflict"])

    def test_version_reporting_reuses_title_cues_and_distinguishes_neither_and_conflict(self) -> None:
        self.connection.execute("UPDATE track_identities SET version_label=NULL, title='Synthetic'")
        self.connection.commit()
        report = dict(MatchEvidenceService(self.store, self.binding.binding_id).report().metrics)
        self.assertEqual(2, report["version_neither_explicit_cue"])
        self.assertEqual(0, report["version_uncertain_or_unavailable"])
        self.connection.execute("UPDATE track_identities SET title='Synthetic Live' WHERE identity_id='identity.unresolved'")
        self.connection.execute("UPDATE track_identities SET title='Synthetic Live' WHERE identity_id='identity.linked.a'")
        self.connection.execute("UPDATE track_identities SET title='Synthetic Acoustic' WHERE identity_id='identity.linked.b'")
        self.connection.commit()
        report = dict(MatchEvidenceService(self.store, self.binding.binding_id).report().metrics)
        self.assertEqual(1, report["version_same_or_compatible"])
        self.assertEqual(1, report["version_explicit_conflict"])

    def test_anomaly_categories_are_read_only_deterministic_and_preserve_multiple_comparisons(self) -> None:
        self.connection.execute("""INSERT INTO canonical_match_candidates(
            candidate_id, account_binding_id, identity_id, canonical_track_id, action, state,
            confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at
        ) VALUES ('candidate.create', ?, 'identity.unresolved', NULL, 'create_new', 'ambiguous', .0, '[]', 'synthetic', 'f', 'new', 'now')""", (self.binding.binding_id,))
        self.connection.commit()
        service = MatchEvidenceReviewService(self.store, self.binding.binding_id)
        before = self.connection.total_changes
        for category, identity_id, duration, isrc in (
            ("same-isrc-exact-duration", "identity.linked.a", 100000, "USRC17607839"),
            ("same-isrc-duration-gt-2s", "identity.linked.a", 103000, "USRC17607839"),
            ("same-isrc-duration-gt-5s", "identity.linked.a", 106000, "USRC17607839"),
            ("different-isrc-duration-le-1s", "identity.linked.b", 100500, "GBAYE6800011"),
            ("different-isrc-exact-duration", "identity.linked.b", 100000, "GBAYE6800011"),
        ):
            with self.subTest(category=category):
                self.connection.execute("UPDATE track_identities SET duration_ms=?, isrc=? WHERE identity_id=?", (duration, isrc, identity_id))
                self.connection.commit()
                rows = service.anomalies(category, 1)
                self.assertEqual(1, len(rows))
                self.assertEqual(category, rows[0].category)
                self.assertEqual("candidate.create", rows[0].paired_create_new_candidate_id)
        self.assertEqual(before + 5, self.connection.total_changes)
        self.connection.execute("UPDATE track_identities SET duration_ms=100000, isrc='USRC17607839' WHERE identity_id IN ('identity.linked.a', 'identity.linked.b')")
        self.connection.commit()
        rows = service.anomalies("same-isrc-exact-duration", 100)
        self.assertEqual(2, len(rows))
        self.assertEqual(tuple(sorted(item.comparison_identity_id for item in rows)), tuple(item.comparison_identity_id for item in rows))

    def test_evidence_list_cli_is_bounded_and_local_only(self) -> None:
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "review-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        runner = CliRunner()
        result = runner.invoke(app, ["catalogue", "match", "evidence-list", "--category", "same-isrc-exact-duration", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("candidate_id: candidate.synthetic", result.output)
        self.assertIn("category: same-isrc-exact-duration", result.output)
        self.assertEqual(1, sum(line.startswith("candidate_id:") for line in result.output.splitlines()))

    def test_tier_a_evaluator_boundaries_vetoes_and_all_comparisons(self) -> None:
        service = TierAEligibilityService(self.store, self.binding.binding_id)
        for delta in (0, 250, 500):
            with self.subTest(delta=delta):
                self.connection.execute("UPDATE track_identities SET duration_ms=?, isrc='USRC17607839' WHERE identity_id='identity.linked.a'", (100000 + delta,))
                self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
                self.connection.commit()
                self.assertTrue(service.evaluate("candidate.synthetic").eligible)
        self.connection.execute("UPDATE track_identities SET duration_ms=100501 WHERE identity_id='identity.linked.a'")
        self.connection.commit()
        self.assertIn("duration_over_threshold", service.evaluate("candidate.synthetic").blocked_reasons)
        self.connection.execute("UPDATE track_identities SET duration_ms=100000, isrc='GBAYE6800011' WHERE identity_id='identity.linked.a'")
        self.connection.commit()
        self.assertIn("isrc_mismatch", service.evaluate("candidate.synthetic").blocked_reasons)
        self.connection.execute("UPDATE track_identities SET isrc='USRC17607839' WHERE identity_id='identity.linked.a'")
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES ('link.restored', ?, 'identity.linked.b', 'canonical.synthetic', 'now')", (self.binding.binding_id,))
        self.connection.commit()
        result = service.evaluate("candidate.synthetic")
        self.assertFalse(result.eligible)
        self.assertIn("comparison_inconsistent", result.blocked_reasons)
        self.assertEqual(
            ("identity.linked.a", "identity.linked.b"),
            tuple(item.identity_id for item in result.comparisons),
        )

    def test_tier_a_fails_closed_for_missing_evidence_version_and_candidate_state(self) -> None:
        service = TierAEligibilityService(self.store, self.binding.binding_id)
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.execute("UPDATE track_identities SET isrc=NULL WHERE identity_id='identity.unresolved'")
        self.connection.commit()
        self.assertIn("isrc_missing", service.evaluate("candidate.synthetic").blocked_reasons)
        self.connection.execute("UPDATE track_identities SET isrc='USRC17607839', duration_ms=NULL WHERE identity_id='identity.unresolved'")
        self.connection.commit()
        self.assertIn("duration_missing", service.evaluate("candidate.synthetic").blocked_reasons)
        self.connection.execute("UPDATE track_identities SET duration_ms=100000, version_label=NULL, title='Synthetic Remix' WHERE identity_id='identity.unresolved'")
        self.connection.commit()
        self.assertIn("version_conflict", service.evaluate("candidate.synthetic").blocked_reasons)
        self.connection.execute("UPDATE canonical_match_candidates SET state='candidate' WHERE candidate_id='candidate.synthetic'")
        self.connection.commit()
        self.assertIn("wrong_candidate_state", service.evaluate("candidate.synthetic").blocked_reasons)

    def test_tier_a_preview_cli_is_local_read_only_and_bounded(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.commit()
        expected_candidates = self.connection.execute(
            "SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id"
        ).fetchall()
        expected_links = self.connection.execute(
            "SELECT identity_id, canonical_track_id FROM account_canonical_identity_links ORDER BY identity_id"
        ).fetchall()
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "tier-a-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        runner = CliRunner()
        result = runner.invoke(app, ["catalogue", "match", "deterministic", "preview", "--eligible-only", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("policy_id: canonical-tier-a.v1", result.output)
        self.assertIn("eligible: 1", result.output)
        self.assertEqual(1, sum(line.startswith("candidate_id:") for line in result.output.splitlines()))
        verification = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(expected_candidates, verification.execute(
            "SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id"
        ).fetchall())
        self.assertEqual(expected_links, verification.execute(
            "SELECT identity_id, canonical_track_id FROM account_canonical_identity_links ORDER BY identity_id"
        ).fetchall())
        verification.close()

    def test_tier_a_apply_reuses_confirmation_and_persists_provenance(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.commit()
        service = TierAEligibilityService(self.store, self.binding.binding_id)
        report = service.apply(("candidate.synthetic",))
        self.assertEqual(1, report.confirmed)
        self.assertEqual("confirmed", self.connection.execute(
            "SELECT state FROM canonical_match_candidates WHERE candidate_id='candidate.synthetic'"
        ).fetchone()[0])
        provenance = self.connection.execute(
            "SELECT policy_id, comparison_count, duration_threshold_ms FROM deterministic_match_decisions"
        ).fetchone()
        self.assertEqual(("canonical-tier-a.v1", 1, 500), tuple(provenance))
        self.assertEqual(0, service.apply(("candidate.synthetic",)).confirmed)

    def test_grouped_candidate_categories_are_read_only_and_expand_all_comparisons(self) -> None:
        service = MatchEvidenceReviewService(self.store, self.binding.binding_id)
        before = self.connection.total_changes
        inconsistent = service.candidate_details("comparison-inconsistent", 1)
        self.assertEqual(1, len(inconsistent))
        self.assertEqual(2, len(inconsistent[0].comparisons))
        self.assertFalse(inconsistent[0].comparison_consistent)
        self.assertEqual(("identity.linked.a", "identity.linked.b"), tuple(x.comparison_identity_id for x in inconsistent[0].comparisons))
        self.assertEqual(before, self.connection.total_changes)
        self.assertEqual(1, len(service.candidate_details("multiple-comparisons", 1)))
        self.assertEqual(0, len(service.candidate_details("multiple-comparisons-all-agree", 1)))

    def test_grouped_candidate_all_agree_and_mixed_band_categories(self) -> None:
        self.connection.execute("UPDATE track_identities SET isrc='USRC17607839', duration_ms=100400 WHERE identity_id='identity.linked.a'")
        self.connection.execute("UPDATE track_identities SET isrc='USRC17607839', duration_ms=100700 WHERE identity_id='identity.linked.b'")
        self.connection.commit()
        service = MatchEvidenceReviewService(self.store, self.binding.binding_id)
        self.assertEqual(0, len(service.candidate_details("multiple-comparisons-all-agree", 1)))
        mixed = service.candidate_details("multiple-comparisons-duration-band-mixed", 1)
        self.assertEqual(1, len(mixed)); self.assertTrue(mixed[0].duration_band_mixed)
        self.connection.execute("UPDATE track_identities SET duration_ms=100450, version_label='live' WHERE identity_id='identity.linked.b'")
        self.connection.commit()
        self.assertEqual(1, len(service.candidate_details("multiple-comparisons-all-agree", 1)))
        self.assertEqual(0, len(service.candidate_details("multiple-comparisons-duration-band-mixed", 1)))

    def test_shared_comparison_consistency_scenarios(self) -> None:
        scenarios = (
            (((True, 500, False), (True, 250, False)), False, False, False, False),
            (((True, 100, False), (False, 100, False)), True, True, False, False),
            (((True, 100, False), (True, 501, False)), True, False, True, False),
            (((True, 100, False), (True, 100, True)), True, False, False, True),
        )
        for evidence, inconsistent, mismatch, over, conflict in scenarios:
            with self.subTest(evidence=evidence):
                facts = evaluate_comparison_consistency(evidence)
                self.assertEqual(2, facts.comparison_count)
                self.assertEqual(inconsistent, facts.comparison_inconsistent)
                self.assertEqual(mismatch, facts.any_isrc_mismatch)
                self.assertEqual(over, facts.any_duration_over_threshold)
                self.assertEqual(conflict, facts.any_version_conflict)

    def test_shared_consistency_consumers_agree_for_fixture_scenarios(self) -> None:
        tier_a = TierAEligibilityService(self.store, self.binding.binding_id)
        evidence = MatchEvidenceReviewService(self.store, self.binding.binding_id)
        cases = (
            ("live", "USRC17607839", 100400, False),
            ("live", "GBAYE6800011", 100100, True),
            ("live", "USRC17607839", 100501, True),
            ("acoustic", "USRC17607839", 100100, True),
        )
        for label, isrc, duration, inconsistent in cases:
            with self.subTest(case=label):
                self.connection.execute("UPDATE track_identities SET version_label=?, isrc=?, duration_ms=? WHERE identity_id='identity.linked.a'", (label, isrc, duration))
                self.connection.execute("UPDATE track_identities SET version_label=?, isrc=?, duration_ms=? WHERE identity_id='identity.linked.b'", (label, isrc, duration))
                self.connection.commit()
                tier = tier_a.evaluate("candidate.synthetic")
                category = "comparison-inconsistent" if inconsistent else "multiple-comparisons-all-agree"
                grouped = evidence.candidate_details(category, 1)
                self.assertEqual(inconsistent, "comparison_inconsistent" in tier.blocked_reasons)
                self.assertEqual(inconsistent, not grouped[0].comparison_consistent)

    def test_identity_external_evidence_round_trips_states_upserts_and_isolates(self) -> None:
        store = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic-query", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2026-02-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        resolved = IdentityExternalEntityEvidence("external.a", "identity.unresolved", external_id="ABCDEFAB-CDEF-CDEF-CDEF-ABCDEFABCDEF", lookup_state="resolved", **base)
        store.upsert(resolved); store.upsert(resolved)
        rows = store.for_identities(("identity.unresolved",))
        self.assertEqual(1, len(rows))
        self.assertEqual("abcdefab-cdef-cdef-cdef-abcdefabcdef", rows[0].external_id)
        self.assertEqual(("identity.unresolved", "musicbrainz", "recording", "resolved", "isrc", "synthetic-query", "synthetic.v1", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"), (rows[0].identity_id, rows[0].provider, rows[0].entity_type, rows[0].lookup_state, rows[0].lookup_method, rows[0].query_fingerprint, rows[0].adapter_version, rows[0].retrieved_at, rows[0].fresh_until))
        ambiguous = IdentityExternalEntityEvidence(
            "external.b",
            "identity.linked.a",
            external_id=None,
            lookup_state="ambiguous",
            **(base | {"ambiguity_count": 2, "ambiguity_digest": "synthetic-digest"}),
        )
        store.upsert(ambiguous)
        self.assertEqual(("identity.linked.a",), tuple(row.identity_id for row in store.for_identities(("identity.linked.a",))))
        self.assertEqual((2, "synthetic-digest"), (store.for_identities(("identity.linked.a",))[0].ambiguity_count, store.for_identities(("identity.linked.a",))[0].ambiguity_digest))
        self.assertEqual(2, len(store.for_identities(("identity.unresolved", "identity.linked.a"))))
        self.assertEqual((), store.for_identities(()))
        self.assertEqual((), store.for_identities(("identity.linked.b",)))
        for state in ("not_found", "insufficient_input", "lookup_failed", "stale"):
            store.upsert(IdentityExternalEntityEvidence("external." + state, "identity.linked.a", external_id=None, lookup_state=state, **base))
            self.assertEqual(state, store.for_identities(("identity.linked.a",))[0].lookup_state)
        with self.assertRaises(Exception):
            store.upsert(IdentityExternalEntityEvidence("bad", "identity.unresolved", external_id=None, lookup_state="resolved", **base))
        with self.assertRaises(Exception):
            store.upsert(IdentityExternalEntityEvidence("bad-count", "identity.unresolved", external_id=None, lookup_state="ambiguous", ambiguity_count=-1, **base))
        with self.assertRaises(Exception):
            store.upsert(IdentityExternalEntityEvidence("bad-state", "identity.unresolved", external_id="ABCDEFAB-CDEF-CDEF-CDEF-ABCDEFABCDEF", lookup_state="not_found", **base))

    def test_identity_external_evidence_cascades_without_matching_mutation(self) -> None:
        store = IdentityExternalEvidenceStore(self.connection)
        self.connection.execute("INSERT INTO track_identities(identity_id, provider, provider_track_id, title, normalized_title, version_label, created_at, duration_ms, isrc) VALUES ('identity.purge', 'spotify', 'provider.purge', 'Synthetic purge', 'synthetic purge', NULL, 'now', NULL, NULL)")
        self.connection.commit()
        item = IdentityExternalEntityEvidence("external.purge", "identity.purge", "musicbrainz", "recording", None, "not_found", "isrc", "synthetic-query", "synthetic.v1", "2026-01-01T00:00:00Z", None, None, None)
        unrelated = IdentityExternalEntityEvidence("external.unrelated", "identity.unresolved", "musicbrainz", "recording", None, "not_found", "isrc", "synthetic-query", "synthetic.v1", "2026-01-01T00:00:00Z", None, None, None)
        before = tuple(self.connection.execute("SELECT canonical_track_id, identity_id FROM account_canonical_identity_links ORDER BY canonical_track_id, identity_id"))
        candidates = tuple(self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id"))
        decisions = tuple(self.connection.execute("SELECT candidate_id FROM deterministic_match_decisions ORDER BY candidate_id"))
        store.upsert(unrelated)
        store.upsert(item)
        self.assertEqual((item,), store.for_identities(("identity.purge",)))
        self.connection.execute("DELETE FROM track_identities WHERE identity_id='identity.purge'")
        self.connection.commit()
        self.assertEqual((), store.for_identities(("identity.purge",)))
        self.assertEqual(before, tuple(self.connection.execute("SELECT canonical_track_id, identity_id FROM account_canonical_identity_links ORDER BY canonical_track_id, identity_id")))
        self.assertEqual(candidates, tuple(self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id")))
        self.assertEqual(decisions, tuple(self.connection.execute("SELECT candidate_id FROM deterministic_match_decisions ORDER BY candidate_id")))
        self.assertEqual(("identity.unresolved",), tuple(row.identity_id for row in store.for_identities(("identity.unresolved", "identity.linked.a", "identity.purge"))))

    def test_identity_external_evidence_schema_is_minimal_and_network_free(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(identity_external_entity_evidence)")}
        self.assertFalse(columns.intersection({"raw_response", "response_body", "artwork", "release_metadata", "title", "artist", "album", "payload_json"}))
        self.assertEqual(
            "CASCADE",
            self.connection.execute("PRAGMA foreign_key_list(identity_external_entity_evidence)").fetchone()[6],
        )

    def test_external_recording_target_projection_reuses_current_topology_and_deduplicates(self) -> None:
        self.connection.execute("INSERT INTO track_identities(identity_id, provider, provider_track_id, title, normalized_title, version_label, created_at, duration_ms, isrc) VALUES ('identity.second', 'spotify', 'provider.second', 'Synthetic second', 'synthetic second', NULL, 'now', 100000, 'USRC17607839')")
        self.connection.execute("INSERT INTO account_track_identity_state(account_binding_id, identity_id, first_seen_scan_id, first_seen_source_id, first_published_scan_id, first_published_source_id, preexisting, last_observed_at) VALUES (?, 'identity.second', 'scan.synthetic', 'source.synthetic', 'scan.synthetic', 'source.synthetic', 0, 'now')", (self.binding.binding_id,))
        self.connection.execute("INSERT INTO canonical_match_candidates(candidate_id, account_binding_id, identity_id, canonical_track_id, action, state, confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at) VALUES ('candidate.second', ?, 'identity.second', 'canonical.synthetic', 'link_existing', 'ambiguous', .5, '[]', 'synthetic.v1', 'fingerprint.second', 'key.second', 'later')", (self.binding.binding_id,))
        self.connection.execute("INSERT INTO canonical_match_candidates(candidate_id, account_binding_id, identity_id, canonical_track_id, action, state, confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at) VALUES ('candidate.paired', ?, 'identity.unresolved', NULL, 'create_new', 'ambiguous', .5, '[]', 'synthetic.v1', 'fingerprint.paired', 'key.paired', 'later')", (self.binding.binding_id,))
        self.connection.execute("INSERT INTO canonical_match_candidates(candidate_id, account_binding_id, identity_id, canonical_track_id, action, state, confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at) VALUES ('candidate.resolved', ?, 'identity.linked.a', 'canonical.synthetic', 'link_existing', 'ambiguous', .5, '[]', 'synthetic.v1', 'fingerprint.resolved', 'key.resolved', 'later')", (self.binding.binding_id,))
        self.connection.commit()
        before = self.connection.total_changes
        preview = ExternalRecordingTargetProjectionService(self.store, self.binding.binding_id).preview()
        self.assertEqual(2, preview.current_candidate_count)
        self.assertEqual(2, preview.unique_candidate_identity_count)
        self.assertEqual(2, preview.unique_comparison_identity_count)
        self.assertEqual(4, preview.unique_target_identity_count)
        self.assertEqual(0, preview.candidate_and_comparison_overlap)
        self.assertEqual(("identity.linked.a", "identity.linked.b", "identity.second", "identity.unresolved"), tuple(item.identity_id for item in preview.targets))
        self.assertEqual(("candidate",), preview.targets[-1].roles)
        self.assertEqual(("comparison",), preview.targets[0].roles)
        self.assertEqual(before, self.connection.total_changes)

    def test_external_recording_target_projection_classifies_evidence_without_mutation(self) -> None:
        store = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="synthetic", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        store.upsert(IdentityExternalEntityEvidence("ext.resolved", "identity.unresolved", external_id="ABCDEFAB-CDEF-CDEF-CDEF-ABCDEFABCDEF", lookup_state="resolved", fresh_until="2099-01-01T00:00:00Z", **base))
        store.upsert(IdentityExternalEntityEvidence("ext.ambiguous", "identity.linked.a", external_id=None, lookup_state="ambiguous", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=2, ambiguity_digest="synthetic", **{key: value for key, value in base.items() if key not in {"ambiguity_count", "ambiguity_digest"}}))
        store.upsert(IdentityExternalEntityEvidence("ext.stale", "identity.linked.b", external_id=None, lookup_state="not_found", fresh_until="2020-01-01T00:00:00Z", **base))
        self.connection.execute("INSERT INTO identity_external_entity_evidence VALUES ('ext.other', 'identity.linked.b', 'other', 'recording', NULL, 'not_found', 'synthetic', 'synthetic', 'synthetic.v1', '2026-01-01T00:00:00Z', NULL, NULL, NULL)")
        self.connection.commit()
        before = self.connection.total_changes
        preview = ExternalRecordingTargetProjectionService(self.store, self.binding.binding_id).preview()
        targets = {item.identity_id: item for item in preview.targets}
        self.assertEqual(("fresh_resolved", False, "abcdefab-cdef-cdef-cdef-abcdefabcdef"), (targets["identity.unresolved"].evidence_state, targets["identity.unresolved"].requires_lookup, targets["identity.unresolved"].external_recording_id))
        self.assertEqual(("fresh_ambiguous", False), (targets["identity.linked.a"].evidence_state, targets["identity.linked.a"].requires_lookup))
        self.assertEqual(("stale", True), (targets["identity.linked.b"].evidence_state, targets["identity.linked.b"].requires_lookup))
        for state, expected_state, requires_lookup in (
            ("not_found", "fresh_not_found", False),
            ("insufficient_input", "fresh_insufficient_input", False),
            ("lookup_failed", "fresh_lookup_failed", False),
            ("stale", "stale", True),
        ):
            with self.subTest(state=state):
                item = IdentityExternalEntityEvidence("ext." + state, "identity.synthetic", "musicbrainz", "recording", None, state, "synthetic", "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z", None, None)
                target = ExternalRecordingTargetProjectionService._target("identity.synthetic", 1, 0, item, datetime.now(UTC))
                self.assertEqual((expected_state, requires_lookup), (target.evidence_state, target.requires_lookup))
        self.assertEqual(before, self.connection.total_changes)

    def test_external_entity_detail_store_replaces_normalized_current_detail(self) -> None:
        store = ExternalEntityDetailStore(self.connection)
        recording_id = "ABCDEFAB-CDEF-CDEF-CDEF-ABCDEFABCDEF"
        def detail(state: str = "resolved", available: bool = True, isrcs: tuple[str, ...] = ("us-rc1-76-07839", "GBAYE6800011"), length: int | None = 100000, fresh_until: str | None = "2099-01-01T00:00:00Z") -> ExternalEntityDetailEvidence:
            normalized = tuple(sorted({value.replace("-", "").upper() for value in isrcs})) if available else ()
            return ExternalEntityDetailEvidence(
                "detail.synthetic", "musicbrainz", "recording", recording_id, state,
                "recording_detail", "synthetic-query", "synthetic.v1", "2026-01-01T00:00:00Z",
                fresh_until, detail_content_digest(isrcs_available=available, associated_isrcs=normalized, recording_length_ms=length), available, length,
            )
        store.upsert(detail(isrcs=("GBAYE6800011", "us-rc1-76-07839", "USRC17607839")), ("GBAYE6800011", "us-rc1-76-07839", "USRC17607839"))
        current = store.get("musicbrainz", "recording", recording_id)
        self.assertEqual(("abcdefab-cdef-cdef-cdef-abcdefabcdef", True, 100000), (current.external_id, current.isrcs_available, current.recording_length_ms))
        self.assertEqual(("GBAYE6800011", "USRC17607839"), tuple(item.normalized_isrc for item in store.isrc_assertions("detail.synthetic")))
        store.upsert(detail(available=True, isrcs=(), length=None), ())
        self.assertEqual((), store.isrc_assertions("detail.synthetic"))
        store.upsert(detail(available=False, isrcs=(), length=None), ())
        self.assertFalse(store.get("musicbrainz", "recording", recording_id).isrcs_available)
        self.assertEqual(("abcdefab-cdef-cdef-cdef-abcdefabcdef",), tuple(item.external_id for item in store.for_entities("musicbrainz", "recording", (recording_id,))))
        with self.assertRaises(ValueError):
            store.upsert(detail(isrcs=("invalid",)), ("invalid",))
        self.connection.execute("DELETE FROM external_entity_detail_evidence WHERE evidence_id='detail.synthetic'")
        self.connection.commit()
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM external_entity_detail_isrc_assertions").fetchone()[0])
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(external_entity_detail_evidence)")}
        self.assertFalse(columns.intersection({"raw_response", "response_body", "payload_json", "metadata"}))

    def test_external_entity_detail_store_preserves_resolved_detail_on_lookup_failure(self) -> None:
        store = ExternalEntityDetailStore(self.connection)
        recording_id = "abcdefab-cdef-cdef-cdef-abcdefabcdef"
        resolved = ExternalEntityDetailEvidence("detail.resolved", "musicbrainz", "recording", recording_id, "resolved", "recording_detail", "query", "synthetic.v1", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z", detail_content_digest(isrcs_available=True, associated_isrcs=("USRC17607839",), recording_length_ms=100000), True, 100000)
        failed = ExternalEntityDetailEvidence("detail.failed", "musicbrainz", "recording", recording_id, "lookup_failed", "recording_detail", "query.failed", "synthetic.v1", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", detail_content_digest(isrcs_available=False, associated_isrcs=(), recording_length_ms=None), False, None)
        store.upsert(resolved, ("USRC17607839",)); store.upsert(failed)
        self.assertEqual("detail.resolved", store.get("musicbrainz", "recording", recording_id).evidence_id)

    def test_external_recording_detail_target_projection_and_preview_cli_are_bounded_local(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.execute("UPDATE track_identities SET isrc='GBAYE6800011', duration_ms=100500, version_label=NULL, title='Synthetic' WHERE identity_id='identity.linked.a'")
        self.connection.execute("UPDATE track_identities SET title='Synthetic' WHERE identity_id='identity.unresolved'")
        self.connection.commit()
        evidence = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        recording_id = "abcdefab-cdef-cdef-cdef-abcdefabcdef"
        evidence.upsert(IdentityExternalEntityEvidence("external.detail.resolved", "identity.unresolved", external_id=recording_id, lookup_state="resolved", **base))
        evidence.upsert(IdentityExternalEntityEvidence("external.detail.not-found", "identity.linked.a", external_id=None, lookup_state="not_found", **base))
        before = self.connection.total_changes
        preview = ExternalRecordingDetailTargetProjectionService(self.store, self.binding.binding_id).preview()
        self.assertEqual((1, 1, 1, 0, 1, "fresh_not_found", "no_evidence", True), (preview.eligible_candidate_references, preview.unique_recording_targets, preview.fresh_not_found_counterpart_targets, preview.fresh_ambiguous_counterpart_targets, preview.requires_lookup_targets, preview.targets[0].counterpart_state, preview.targets[0].detail_evidence_state, preview.targets[0].requires_lookup))
        self.assertEqual(before, self.connection.total_changes)
        database = Path(self.temp.name) / "app.db"; data_dir = Path(self.temp.name) / "detail-preview-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        args = ["catalogue", "match", "external-recording", "detail", "preview", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)]
        first = CliRunner().invoke(app, args); second = CliRunner().invoke(app, args)
        self.assertEqual(0, first.exit_code, first.output); self.assertEqual(first.output, second.output)
        for field in ("eligible_candidate_references: 1", "unique_recording_targets: 1", "selected_targets: 1", "network_access: no", "matching_mutation: no", "external_recording_id:"):
            self.assertIn(field, first.output)
        self.assertNotEqual(0, CliRunner().invoke(app, [*args[:6], "--limit", "0", *args[8:]]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, [*args[:6], "--limit", "21", *args[8:]]).exit_code)

    def test_external_recording_preview_cli_is_bounded_deterministic_and_read_only(self) -> None:
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "external-recording-runtime"; (data_dir / "state").mkdir(parents=True)
        expected_candidates = self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall()
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        args = ["catalogue", "match", "external-recording", "preview", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)]
        first = CliRunner().invoke(app, args)
        second = CliRunner().invoke(app, args)
        self.assertEqual(0, first.exit_code, first.output)
        self.assertEqual(first.output, second.output)
        self.assertIn("current_candidates: 1", first.output)
        self.assertIn("unique_target_identities: 3", first.output)
        self.assertEqual(1, sum(line.startswith("identity_id:") for line in first.output.splitlines()))
        self.assertNotEqual(0, CliRunner().invoke(app, [*args[:5], "--limit", "0", *args[5:]]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, [*args[:5], "--limit", "101", *args[5:]]).exit_code)
        verification = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(expected_candidates, verification.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall())
        verification.close()

    def test_external_recording_fetch_is_bounded_persists_evidence_and_preserves_matching(self) -> None:
        self.connection.execute("UPDATE track_identities SET isrc=NULL WHERE identity_id='identity.linked.b'")
        self.connection.commit()
        candidates = self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall()
        class Lookup:
            calls: list[str] = []
            def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
                self.calls.append(isrc)
                ids = ("abcdefab-cdef-cdef-cdef-abcdefabcdef",) if isrc == "USRC17607839" else ()
                return MusicBrainzRecordingLookupResult(isrc, ids, "synthetic-fingerprint", "synthetic.v1", "2026-01-01T00:00:00Z")
        lookup = Lookup()
        service = ExternalRecordingEvidenceFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        report = service.fetch(3, lookup)
        self.assertEqual((3, 3, 2, 0, 0, 1, 0, 0), (report.planned, report.processed, report.resolved, report.ambiguous, report.not_found, report.insufficient_input, report.lookup_failed, report.skipped_fresh))
        self.assertEqual(("USRC17607839", "USRC17607839"), tuple(lookup.calls))
        rows = IdentityExternalEvidenceStore(self.connection).for_identities(("identity.linked.a", "identity.linked.b", "identity.unresolved"))
        self.assertEqual(("resolved", "insufficient_input", "resolved"), tuple(item.lookup_state for item in rows))
        self.assertEqual(candidates, self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall())

    def test_external_recording_fetch_cli_requires_confirmation_and_is_bounded(self) -> None:
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "external-recording-fetch-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        args = ["catalogue", "match", "external-recording", "fetch", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)]
        declined = CliRunner().invoke(app, args, input="n\n")
        self.assertNotEqual(0, declined.exit_code)
        self.assertIn("network_access: yes", declined.output)
        verification = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(0, verification.execute("SELECT COUNT(*) FROM identity_external_entity_evidence").fetchone()[0])
        verification.close()
        class FakeProvider:
            def __init__(self, **_: object) -> None: pass
            def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
                return MusicBrainzRecordingLookupResult(isrc, ("abcdefab-cdef-cdef-cdef-abcdefabcdef",), "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z")
        with patch("spotify_playlist_taxonomy.cli.MusicBrainzResearchProvider", FakeProvider):
            accepted = CliRunner().invoke(app, [*args, "--yes"])
        self.assertEqual(0, accepted.exit_code, accepted.output)
        self.assertIn("planned: 1", accepted.output)
        self.assertIn("matching_mutations: 0", accepted.output)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "fetch", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)

    def test_external_recording_comparison_partitions_candidates_and_flags(self) -> None:
        store = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        for identity_id in ("identity.unresolved", "identity.linked.a", "identity.linked.b"):
            store.upsert(IdentityExternalEntityEvidence("external." + identity_id, identity_id, external_id="abcdefab-cdef-cdef-cdef-abcdefabcdef", lookup_state="resolved", **base))
        report = ExternalRecordingComparisonService(self.store, self.binding.binding_id).status()
        self.assertEqual(("all-resolved-same-recording",), tuple(item.primary_outcome for item in report.comparisons))
        self.assertEqual(1, sum(dict(report.primary_counts).values()))
        store.upsert(IdentityExternalEntityEvidence("external.ambiguous", "identity.linked.b", external_id=None, lookup_state="ambiguous", **(base | {"ambiguity_count": 2, "ambiguity_digest": "synthetic"})))
        item = ExternalRecordingComparisonService(self.store, self.binding.binding_id).status().comparisons[0]
        self.assertEqual("partial-resolution", item.primary_outcome)
        self.assertTrue(item.has_ambiguous)

    def test_external_recording_comparison_context_reuses_local_evidence_for_all_members(self) -> None:
        store = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        recording_a = "abcdefab-cdef-cdef-cdef-abcdefabcdef"; recording_b = "11111111-2222-3333-4444-555555555555"
        for identity_id, recording_id in (("identity.unresolved", recording_a), ("identity.linked.a", recording_a), ("identity.linked.b", recording_b)):
            store.upsert(IdentityExternalEntityEvidence("external.context." + identity_id, identity_id, external_id=recording_id, lookup_state="resolved", **base))
        before = self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall()
        item = ExternalRecordingComparisonService(self.store, self.binding.binding_id).compare("all-resolved-disagree", 1)[0]
        self.assertEqual(("all-resolved-disagree", "identity.unresolved", "Synthetic", ("Artist",), "live", 100000, "USRC17607839"), (item.primary_outcome, item.candidate.identity_id, item.candidate.title, item.candidate.artists, item.candidate.effective_version_cue, item.candidate.duration_ms, item.candidate.isrc))
        self.assertEqual(("identity.linked.a", "identity.linked.b"), tuple(member.identity_id for member in item.comparisons))
        self.assertEqual((0, "same", "compatible", "consistent", "same"), (item.comparisons[0].duration_delta_ms, item.comparisons[0].isrc_relationship, item.comparisons[0].version_relationship, item.comparisons[0].comparison_consistency, item.comparisons[0].external_recording_relationship))
        self.assertEqual((6001, "different", "conflict", "inconsistent", "different"), (item.comparisons[1].duration_delta_ms, item.comparisons[1].isrc_relationship, item.comparisons[1].version_relationship, item.comparisons[1].comparison_consistency, item.comparisons[1].external_recording_relationship))
        self.assertEqual((3, 2, True), (item.resolved_identity_count, item.distinct_resolved_recording_count, item.comparison_consistency.comparison_inconsistent))
        store.upsert(IdentityExternalEntityEvidence("external.context.same", "identity.linked.b", external_id=recording_a, lookup_state="resolved", **base))
        same = ExternalRecordingComparisonService(self.store, self.binding.binding_id).compare("all-resolved-same-recording", 1)[0]
        self.assertEqual("same", same.comparisons[1].external_recording_relationship)
        store.upsert(IdentityExternalEntityEvidence("external.context.not-found", "identity.linked.b", external_id=None, lookup_state="not_found", **base))
        partial = ExternalRecordingComparisonService(self.store, self.binding.binding_id).compare("partial-resolution", 1)[0]
        self.assertEqual((3, "fresh_not_found", "unavailable"), (len(partial.members), partial.comparisons[1].evidence_state, partial.comparisons[1].external_recording_relationship))
        self.connection.execute("UPDATE track_identities SET version_label=NULL, title='Synthetic' WHERE identity_id='identity.linked.b'"); self.connection.commit()
        unknown_version = ExternalRecordingComparisonService(self.store, self.binding.binding_id).compare("partial-resolution", 1)[0]
        self.assertEqual("unknown", unknown_version.comparisons[1].version_relationship)
        self.connection.execute("UPDATE track_identities SET isrc=NULL WHERE identity_id='identity.linked.b'"); self.connection.commit()
        missing = ExternalRecordingComparisonService(self.store, self.binding.binding_id).compare("partial-resolution", 1)[0]
        self.assertEqual("missing", missing.comparisons[1].isrc_relationship)
        self.assertEqual(before, self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall())

    def test_external_recording_relationship_requires_two_usable_fresh_resolutions(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'"); self.connection.commit()
        evidence = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        recording_a = "abcdefab-cdef-cdef-cdef-abcdefabcdef"; recording_b = "11111111-2222-3333-4444-555555555555"
        def set_state(identity_id: str, state: str, recording_id: str | None = None) -> None:
            if state == "no_evidence":
                self.connection.execute("DELETE FROM identity_external_entity_evidence WHERE identity_id=?", (identity_id,)); self.connection.commit(); return
            lookup_state = "not_found" if state == "stale" else state.removeprefix("fresh_")
            extra = {"fresh_until": "2000-01-01T00:00:00Z"} if state == "stale" else {}
            if lookup_state == "ambiguous": extra |= {"ambiguity_count": 2, "ambiguity_digest": "synthetic"}
            evidence.upsert(IdentityExternalEntityEvidence("external.relationship." + identity_id, identity_id, external_id=recording_id, lookup_state=lookup_state, **(base | extra)))
        def current() -> tuple[str, str]:
            item = ExternalRecordingComparisonService(self.store, self.binding.binding_id).status().comparisons[0]
            return item.primary_outcome, item.comparisons[0].external_recording_relationship or ""
        set_state("identity.unresolved", "fresh_resolved", recording_a); set_state("identity.linked.a", "fresh_resolved", recording_a)
        self.assertEqual(("all-resolved-same-recording", "same"), current())
        set_state("identity.linked.a", "fresh_resolved", recording_b)
        self.assertEqual(("all-resolved-disagree", "different"), current())
        for candidate_state, candidate_id, comparison_state, comparison_id in (
            ("fresh_resolved", recording_a, "fresh_not_found", None),
            ("fresh_not_found", None, "fresh_resolved", recording_a),
            ("fresh_not_found", None, "no_evidence", None),
            ("no_evidence", None, "fresh_not_found", None),
            ("fresh_ambiguous", None, "no_evidence", None),
            ("fresh_lookup_failed", None, "fresh_lookup_failed", None),
            ("stale", None, "stale", None),
        ):
            with self.subTest(candidate_state=candidate_state, comparison_state=comparison_state):
                set_state("identity.unresolved", candidate_state, candidate_id); set_state("identity.linked.a", comparison_state, comparison_id)
                self.assertEqual("unavailable", current()[1])
        impossible = ExternalRecordingComparisonMember("identity.impossible", "Synthetic", (), None, None, None, "fresh_resolved", None)
        resolved = ExternalRecordingComparisonMember("identity.resolved", "Synthetic", (), None, None, None, "fresh_resolved", recording_a)
        self.assertEqual("unavailable", _external_recording_relationship(impossible, resolved))
        self.assertEqual("unavailable", _external_recording_relationship(impossible, impossible))

    def test_external_recording_status_and_compare_cli_are_local_and_bounded(self) -> None:
        database = Path(self.temp.name) / "app.db"; data_dir = Path(self.temp.name) / "external-status-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        status = CliRunner().invoke(app, ["catalogue", "match", "external-recording", "status", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        compare = CliRunner().invoke(app, ["catalogue", "match", "external-recording", "compare", "--category", "no-resolution", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, status.exit_code, status.output); self.assertEqual(0, compare.exit_code, compare.output)
        self.assertIn("current_candidates: 1", status.output); self.assertEqual(1, sum(line.startswith("candidate_id:") for line in compare.output.splitlines()))

    def test_external_recording_compare_cli_renders_context_without_writes(self) -> None:
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        evidence = IdentityExternalEvidenceStore(self.connection)
        for identity_id, recording_id in (("identity.unresolved", "abcdefab-cdef-cdef-cdef-abcdefabcdef"), ("identity.linked.a", "abcdefab-cdef-cdef-cdef-abcdefabcdef"), ("identity.linked.b", "11111111-2222-3333-4444-555555555555")):
            evidence.upsert(IdentityExternalEntityEvidence("external.cli." + identity_id, identity_id, external_id=recording_id, lookup_state="resolved", **base))
        database = Path(self.temp.name) / "app.db"; data_dir = Path(self.temp.name) / "external-context-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        before_connection = connect_database(data_dir / "state" / "app.db")
        before = before_connection.execute("SELECT COUNT(*) FROM identity_external_entity_evidence").fetchone()[0]
        before_connection.close()
        result = CliRunner().invoke(app, ["catalogue", "match", "external-recording", "compare", "--category", "all-resolved-disagree", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        after_connection = connect_database(data_dir / "state" / "app.db"); after = after_connection.execute("SELECT COUNT(*) FROM identity_external_entity_evidence").fetchone()[0]; after_connection.close()
        self.assertEqual(0, result.exit_code, result.output); self.assertEqual(before, after)
        for field in ("candidate_identity_id:", "candidate_title:", "candidate_artists:", "candidate_effective_version_cue:", "comparison_1_identity_id:", "comparison_1_duration_delta_ms:", "comparison_1_isrc_relationship:", "comparison_1_version_relationship:", "comparison_1_comparison_consistency:", "comparison_1_external_recording_relationship:"):
            self.assertIn(field, result.output)

    def test_external_recording_candidate_stratum_matches_exact_local_boundaries(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.execute("UPDATE track_identities SET isrc='GBAYE6800011', duration_ms=100000, version_label=NULL, title='Synthetic' WHERE identity_id='identity.linked.a'")
        self.connection.execute("UPDATE track_identities SET title='Synthetic' WHERE identity_id='identity.unresolved'")
        self.connection.commit()
        service = ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        plan = service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM)
        self.assertEqual((1, 1, 0, 0, 1, 2, 2), (plan.stratum_matching_candidates, plan.stratum_completable_candidates, plan.stratum_blocked_candidates, plan.stratum_completed_candidates, len(plan.selected_bundles), len(plan.required_lookup_identity_ids), len(plan.selected_bundles[0].member_identity_ids)))
        for updates in (
            "duration_ms=100500",
            "duration_ms=100501",
            "isrc='USRC17607839', duration_ms=100000",
            "isrc=NULL, duration_ms=100000",
            "isrc='GBAYE6800011', duration_ms=NULL",
            "duration_ms=100000, version_label='studio'",
        ):
            self.connection.execute(f"UPDATE track_identities SET {updates} WHERE identity_id='identity.linked.a'"); self.connection.commit()
            expected = 0 if updates in {"duration_ms=100501", "isrc='USRC17607839', duration_ms=100000", "isrc=NULL, duration_ms=100000", "isrc='GBAYE6800011', duration_ms=NULL", "duration_ms=100000, version_label='studio'"} else 1
            self.assertEqual(expected, service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM).stratum_matching_candidates, updates)
        self.connection.execute("UPDATE track_identities SET isrc='GBAYE6800011', duration_ms=100000, version_label=NULL WHERE identity_id='identity.linked.a'")
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.a'"); self.connection.commit()
        self.assertEqual(0, service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM).stratum_matching_candidates)
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES ('link.restored.a', ?, 'identity.linked.a', 'canonical.synthetic', 'now')", (self.binding.binding_id,))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES ('link.restored.b', ?, 'identity.linked.b', 'canonical.synthetic', 'now')", (self.binding.binding_id,)); self.connection.commit()
        self.assertEqual(0, service.plan(1, 3, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM).stratum_matching_candidates)

    def test_external_recording_candidate_stratum_retains_completion_rules_and_rechecks(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.execute("UPDATE track_identities SET isrc='GBAYE6800011', duration_ms=100000 WHERE identity_id='identity.linked.a'"); self.connection.commit()
        service = ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        evidence = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        self.assertEqual((1, 0, 0), tuple(getattr(service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM), name) for name in ("stratum_completable_candidates", "stratum_blocked_candidates", "stratum_completed_candidates")))
        evidence.upsert(IdentityExternalEntityEvidence("external.resolved.candidate", "identity.unresolved", external_id="abcdefab-cdef-cdef-cdef-abcdefabcdef", lookup_state="resolved", **base))
        partial = service.plan(1, 1, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM)
        self.assertEqual((1, 1, 1), (partial.stratum_completable_candidates, len(partial.required_lookup_identity_ids), partial.already_resolved_members))
        evidence.upsert(IdentityExternalEntityEvidence("external.resolved.comparison", "identity.linked.a", external_id="abcdefab-cdef-cdef-cdef-abcdefabcdef", lookup_state="resolved", **base))
        completed = service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM)
        self.assertEqual((0, 0, 1, 0), (completed.stratum_completable_candidates, completed.stratum_blocked_candidates, completed.stratum_completed_candidates, len(completed.selected_bundles)))
        for state in ("ambiguous", "not_found", "insufficient_input", "lookup_failed"):
            with self.subTest(state=state):
                evidence.upsert(IdentityExternalEntityEvidence("external.terminal." + state, "identity.linked.a", external_id=None, lookup_state=state, **(base | ({"ambiguity_count": 2, "ambiguity_digest": "synthetic"} if state == "ambiguous" else {}))))
                blocked = service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM)
                self.assertEqual((0, 1, 0), (blocked.stratum_completable_candidates, blocked.stratum_blocked_candidates, blocked.stratum_completed_candidates))
        evidence.upsert(IdentityExternalEntityEvidence("external.stale", "identity.linked.a", external_id=None, lookup_state="stale", **base))
        self.assertEqual(1, service.plan(1, 1, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM).stratum_completable_candidates)
        self.connection.execute("DELETE FROM identity_external_entity_evidence"); self.connection.commit()
        plan = service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM)
        self.connection.execute("UPDATE track_identities SET isrc='USRC17607839' WHERE identity_id='identity.linked.a'"); self.connection.commit()
        class NoNetwork:
            calls = 0
            def lookup_recordings_by_isrc(self, isrc: str): self.calls += 1; raise AssertionError("stratum departure must not call provider")
        lookup = NoNetwork(); report = service.fetch(plan, lookup)
        self.assertEqual((0, 1, 0), (report.processed_lookup_identities, report.skipped_candidate_not_in_stratum, report.skipped_candidate_not_completable)); self.assertEqual(0, lookup.calls)
        self.connection.execute("UPDATE track_identities SET isrc='GBAYE6800011' WHERE identity_id='identity.linked.a'"); self.connection.commit()
        terminal_plan = service.plan(1, 2, stratum=EXTERNAL_RECORDING_CANDIDATE_STRATUM)
        evidence.upsert(IdentityExternalEntityEvidence("external.recheck.terminal", "identity.linked.a", external_id=None, lookup_state="not_found", **base))
        terminal = service.fetch(terminal_plan, lookup)
        self.assertEqual((0, 0, 1), (terminal.processed_lookup_identities, terminal.skipped_candidate_not_in_stratum, terminal.skipped_candidate_not_completable)); self.assertEqual(0, lookup.calls)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM deterministic_match_decisions").fetchone()[0])

    def test_external_recording_candidate_stratum_preview_cli_is_read_only_and_bounded(self) -> None:
        self.connection.execute("DELETE FROM account_canonical_identity_links WHERE identity_id='identity.linked.b'")
        self.connection.execute("UPDATE track_identities SET isrc='GBAYE6800011', duration_ms=100000 WHERE identity_id='identity.linked.a'"); self.connection.commit()
        database = Path(self.temp.name) / "app.db"; data_dir = Path(self.temp.name) / "external-stratum-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        args = ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "1", "--identity-limit", "2", "--stratum", EXTERNAL_RECORDING_CANDIDATE_STRATUM, "--preview", "--config", str(CONFIG), "--data-dir", str(data_dir)]
        before_connection = connect_database(data_dir / "state" / "app.db"); before = before_connection.execute("SELECT COUNT(*) FROM identity_external_entity_evidence").fetchone()[0]; before_connection.close()
        preview = CliRunner().invoke(app, args)
        after_connection = connect_database(data_dir / "state" / "app.db"); after = after_connection.execute("SELECT COUNT(*) FROM identity_external_entity_evidence").fetchone()[0]; after_connection.close()
        self.assertEqual(0, preview.exit_code, preview.output); self.assertEqual(before, after)
        for field in (f"stratum: {EXTERNAL_RECORDING_CANDIDATE_STRATUM}", "stratum_matching_candidates: 1", "stratum_completable_candidates: 1", "network_access: no", "required_lookup_identities: 2"):
            self.assertIn(field, preview.output)
        self.assertNotIn("Fetch MusicBrainz", preview.output)
        self.assertNotEqual(0, CliRunner().invoke(app, [*args, "--yes"]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "1", "--identity-limit", "2", "--stratum", "unknown", "--preview", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)
        class FakeProvider:
            def __init__(self, **_: object) -> None: pass
            def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
                return MusicBrainzRecordingLookupResult(isrc, ("abcdefab-cdef-cdef-cdef-abcdefabcdef",), "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z")
        with patch("spotify_playlist_taxonomy.cli.MusicBrainzResearchProvider", FakeProvider):
            accepted = CliRunner().invoke(app, [item for item in args if item != "--preview"] + ["--yes"])
        self.assertEqual(0, accepted.exit_code, accepted.output); self.assertIn("matching_mutations: 0", accepted.output)

    def test_external_recording_candidate_fetch_plan_is_complete_prioritized_and_fail_closed(self) -> None:
        service = ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        plan = service.plan(1, 3)
        self.assertEqual((0, 1, 0, 1, 3, False), (plan.completable_partial_candidates, plan.completable_no_resolution_candidates, plan.blocked_candidates, len(plan.selected_bundles), len(plan.required_lookup_identity_ids), plan.exceeds_identity_limit))
        self.assertEqual(3, len(plan.selected_bundles[0].member_identity_ids))
        self.assertTrue(service.plan(1, 2).exceeds_identity_limit)
        evidence = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        evidence.upsert(IdentityExternalEntityEvidence("external.resolved", "identity.unresolved", external_id="abcdefab-cdef-cdef-cdef-abcdefabcdef", lookup_state="resolved", **base))
        partial = service.plan(1, 2)
        self.assertEqual((1, 0, 1, False), (partial.completable_partial_candidates, partial.completable_no_resolution_candidates, partial.already_resolved_members, partial.exceeds_identity_limit))
        evidence.upsert(IdentityExternalEntityEvidence("external.ambiguous", "identity.linked.a", external_id=None, lookup_state="ambiguous", **(base | {"ambiguity_count": 2, "ambiguity_digest": "synthetic"})))
        blocked = service.plan(1, 3)
        self.assertEqual((0, 0, 1), (blocked.completable_partial_candidates, blocked.completable_no_resolution_candidates, blocked.blocked_candidates))

    def test_external_recording_candidate_fetch_executes_whole_bundle_and_preserves_matching(self) -> None:
        candidates = self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall()
        service = ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        class Lookup:
            calls: list[str] = []
            def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
                self.calls.append(isrc)
                return MusicBrainzRecordingLookupResult(isrc, ("abcdefab-cdef-cdef-cdef-abcdefabcdef",), "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z")
        lookup = Lookup()
        report = service.fetch(service.plan(1, 3), lookup)
        self.assertEqual((1, 1, 3, 3, 3, 0, 0, 0, 0, 0, 0), tuple(getattr(report, name) for name in ("planned_candidates", "processed_candidates", "planned_lookup_identities", "processed_lookup_identities", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh", "skipped_candidate_not_completable")))
        self.assertEqual(3, len(lookup.calls))
        self.assertEqual(candidates, self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id").fetchall())
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM deterministic_match_decisions").fetchone()[0])

    def test_external_recording_candidate_fetch_planner_treats_stale_as_retryable_and_terminal_states_as_blocked(self) -> None:
        service = ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        evidence = IdentityExternalEvidenceStore(self.connection)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        evidence.upsert(IdentityExternalEntityEvidence("external.stale", "identity.linked.a", external_id=None, lookup_state="stale", **base))
        self.assertEqual(1, service.plan(1, 3).completable_no_resolution_candidates)
        for state in ("ambiguous", "not_found", "insufficient_input", "lookup_failed"):
            with self.subTest(state=state):
                evidence.upsert(IdentityExternalEntityEvidence("external." + state, "identity.linked.a", external_id=None, lookup_state=state, **(base | ({"ambiguity_count": 2, "ambiguity_digest": "synthetic"} if state == "ambiguous" else {}))))
                plan = service.plan(1, 3)
                self.assertEqual((0, 0, 1), (plan.completable_partial_candidates, plan.completable_no_resolution_candidates, plan.blocked_candidates))

    def test_external_recording_candidate_fetch_rechecks_candidate_before_network(self) -> None:
        service = ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        plan = service.plan(1, 3)
        base = dict(provider="musicbrainz", entity_type="recording", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=2, ambiguity_digest="synthetic")
        IdentityExternalEvidenceStore(self.connection).upsert(IdentityExternalEntityEvidence("external.terminal", "identity.linked.a", external_id=None, lookup_state="ambiguous", **base))
        class Lookup:
            calls = 0
            def lookup_recordings_by_isrc(self, isrc: str): self.calls += 1; raise AssertionError("must not call provider")
        lookup = Lookup(); report = service.fetch(plan, lookup)
        self.assertEqual((0, 1), (report.processed_lookup_identities, report.skipped_candidate_not_completable))
        self.assertEqual(0, lookup.calls)

    def test_external_recording_candidate_fetch_cli_requires_limits_confirmation_and_respects_overflow(self) -> None:
        database = Path(self.temp.name) / "app.db"; data_dir = Path(self.temp.name) / "external-candidate-fetch-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        base = ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "1", "--identity-limit", "3", "--config", str(CONFIG), "--data-dir", str(data_dir)]
        declined = CliRunner().invoke(app, base, input="n\n")
        self.assertNotEqual(0, declined.exit_code); self.assertIn("selected_candidates: 1", declined.output)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "1", "--identity-limit", "2", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "0", "--identity-limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "21", "--identity-limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "fetch-candidates", "--candidate-limit", "1", "--identity-limit", "101", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)

    def test_external_recording_failed_retry_plan_is_bounded_deterministic_and_ordinary_fetch_remains_closed(self) -> None:
        service = ExternalRecordingEvidenceFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        base = dict(provider="musicbrainz", entity_type="recording", external_id=None, lookup_state="lookup_failed", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        evidence = IdentityExternalEvidenceStore(self.connection)
        for identity_id in ("identity.unresolved", "identity.linked.b"):
            evidence.upsert(IdentityExternalEntityEvidence("external." + identity_id, identity_id, **base))
        for state in ("resolved", "ambiguous", "not_found", "insufficient_input"):
            evidence.upsert(IdentityExternalEntityEvidence(
                "external.excluded." + state,
                "identity.linked.a",
                **(base | {"external_id": "abcdefab-cdef-cdef-cdef-abcdefabcdef" if state == "resolved" else None, "lookup_state": state}),
            ))
            self.assertNotIn("identity.linked.a", {item.identity_id for item in service.failed_retry_plan(100)})
        evidence.upsert(IdentityExternalEntityEvidence("external.excluded.stale", "identity.linked.a", **(base | {"fresh_until": "2000-01-01T00:00:00Z"})))
        self.assertNotIn("identity.linked.a", {item.identity_id for item in service.failed_retry_plan(100)})
        self.connection.execute("DELETE FROM identity_external_entity_evidence WHERE identity_id='identity.linked.a'")
        evidence.upsert(IdentityExternalEntityEvidence("external.excluded.provider", "identity.linked.a", "wikidata", "recording", None, "lookup_failed", "isrc", "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z", None, None))
        self.assertNotIn("identity.linked.a", {item.identity_id for item in service.failed_retry_plan(100)})
        self.assertEqual(("identity.linked.b", "identity.unresolved"), tuple(item.identity_id for item in service.failed_retry_plan(2)))
        self.assertEqual(("identity.linked.b",), tuple(item.identity_id for item in service.failed_retry_plan(1)))
        self.assertTrue({"identity.unresolved", "identity.linked.b"}.isdisjoint({item.identity_id for item in service.plan(100)}))
        targets = {item.identity_id: item for item in ExternalRecordingTargetProjectionService(self.store, self.binding.binding_id).preview().targets}
        self.assertFalse(targets["identity.linked.b"].requires_lookup)
        self.assertTrue(ExternalRecordingCandidateFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1).plan(1, 3).blocked_candidates > 0)

    def test_external_recording_failed_retry_reuses_fetch_persistence_and_rechecks_state(self) -> None:
        service = ExternalRecordingEvidenceFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        base = dict(provider="musicbrainz", entity_type="recording", external_id=None, lookup_state="lookup_failed", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        evidence = IdentityExternalEvidenceStore(self.connection); evidence.upsert(IdentityExternalEntityEvidence("external.failed", "identity.unresolved", **base))
        planned = service.failed_retry_plan(1); candidates = self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates").fetchall()
        class Lookup:
            calls = 0
            def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
                self.calls += 1; return MusicBrainzRecordingLookupResult(isrc, ("abcdefab-cdef-cdef-cdef-abcdefabcdef",), "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z")
        lookup = Lookup(); report = service.retry_failed_targets(planned, lookup)
        self.assertEqual((1, 1, 1, 0), (report.planned, report.processed, report.resolved, report.skipped_not_failed)); self.assertEqual(1, lookup.calls)
        self.assertEqual("resolved", IdentityExternalEvidenceStore(self.connection).for_identities(("identity.unresolved",))[0].lookup_state)
        self.assertEqual(candidates, self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates").fetchall())
        evidence.upsert(IdentityExternalEntityEvidence("external.failed.again", "identity.unresolved", **base)); stale_plan = service.failed_retry_plan(1)
        evidence.upsert(IdentityExternalEntityEvidence("external.changed", "identity.unresolved", **(base | {"lookup_state": "not_found"})))
        skipped = service.retry_failed_targets(stale_plan, lookup)
        self.assertEqual((0, 1), (skipped.processed, skipped.skipped_not_failed)); self.assertEqual(1, lookup.calls)

    def test_external_recording_failed_retry_handles_terminal_outcomes_missing_input_and_topology_change(self) -> None:
        service = ExternalRecordingEvidenceFetchService(self.store, self.binding.binding_id, positive_ttl_hours=1, negative_ttl_hours=1, ambiguous_ttl_hours=1)
        base = dict(provider="musicbrainz", entity_type="recording", external_id=None, lookup_state="lookup_failed", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        evidence = IdentityExternalEvidenceStore(self.connection)
        for recordings, expected in (((), "not_found"), (("abcdefab-cdef-cdef-cdef-abcdefabcdef", "11111111-2222-3333-4444-555555555555"), "ambiguous")):
            evidence.upsert(IdentityExternalEntityEvidence("external.retry." + expected, "identity.unresolved", **base))
            class Lookup:
                def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult: return MusicBrainzRecordingLookupResult(isrc, recordings, "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z")
            self.assertEqual(1, getattr(service.retry_failed(1, Lookup()), expected))
        evidence.upsert(IdentityExternalEntityEvidence("external.retry.input", "identity.unresolved", **base)); self.connection.execute("UPDATE track_identities SET isrc=NULL WHERE identity_id='identity.unresolved'"); self.connection.commit()
        class NoNetwork:
            def lookup_recordings_by_isrc(self, isrc: str): raise AssertionError("missing ISRC must not call provider")
        self.assertEqual(1, service.retry_failed(1, NoNetwork()).insufficient_input)
        self.connection.execute("UPDATE track_identities SET isrc='USRC17607839' WHERE identity_id='identity.unresolved'"); evidence.upsert(IdentityExternalEntityEvidence("external.retry.topology", "identity.unresolved", **base)); planned = service.failed_retry_plan(1)
        self.connection.execute("DELETE FROM canonical_match_candidates WHERE candidate_id='candidate.synthetic'"); self.connection.commit()
        topology_skipped = service.retry_failed_targets(planned, NoNetwork())
        self.assertEqual((0, 1), (topology_skipped.processed, topology_skipped.skipped_not_failed))

    def test_external_recording_retry_failed_cli_is_confirmed_bounded_and_synthetic(self) -> None:
        base = dict(provider="musicbrainz", entity_type="recording", external_id=None, lookup_state="lookup_failed", lookup_method="isrc", query_fingerprint="synthetic", adapter_version="synthetic.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", ambiguity_count=None, ambiguity_digest=None)
        IdentityExternalEvidenceStore(self.connection).upsert(IdentityExternalEntityEvidence("external.failed", "identity.unresolved", **base))
        database = Path(self.temp.name) / "app.db"; data_dir = Path(self.temp.name) / "external-retry-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        args = ["catalogue", "match", "external-recording", "retry-failed", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)]
        declined = CliRunner().invoke(app, args, input="n\n"); self.assertNotEqual(0, declined.exit_code); self.assertIn("eligible_failed_targets: 1", declined.output)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "retry-failed", "--limit", "0", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)
        self.assertNotEqual(0, CliRunner().invoke(app, ["catalogue", "match", "external-recording", "retry-failed", "--limit", "101", "--config", str(CONFIG), "--data-dir", str(data_dir)]).exit_code)
        class FakeProvider:
            def __init__(self, **_: object) -> None: pass
            def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
                return MusicBrainzRecordingLookupResult(isrc, (), "synthetic", "synthetic.v1", "2026-01-01T00:00:00Z")
        with patch("spotify_playlist_taxonomy.cli.MusicBrainzResearchProvider", FakeProvider): accepted = CliRunner().invoke(app, [*args, "--yes"])
        self.assertEqual(0, accepted.exit_code, accepted.output); self.assertIn("not_found: 1", accepted.output); self.assertIn("matching_mutations: 0", accepted.output)

    def test_grouped_evidence_cli_renders_complete_synthetic_evidence(self) -> None:
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "grouped-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close(); database.replace(data_dir / "state" / "app.db")
        result = CliRunner().invoke(app, ["catalogue", "match", "evidence-list", "--category", "comparison-inconsistent", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        for field in ("candidate_id:", "identity_id:", "identity:", "identity_version_cue: live", "identity_duration_ms:", "identity_isrc:", "canonical_id:", "comparison_count: 2", "comparison_consistent:", "comparison_inconsistent:", "min_duration_delta_ms:", "max_duration_delta_ms:", "duration_band_mixed:", "comparison_1_identity_id:", "comparison_1:", "comparison_1_version_cue:", "comparison_1_duration_ms:", "comparison_1_isrc:", "comparison_1_duration_delta_ms:", "comparison_1_same_isrc:", "comparison_1_duration_band:", "comparison_2_identity_id:", "comparison_2:", "comparison_2_version_cue:", "comparison_2_duration_ms:", "comparison_2_isrc:", "comparison_2_duration_delta_ms:", "comparison_2_same_isrc:", "comparison_2_duration_band:"):
            self.assertIn(field, result.output)
        self.assertEqual(1, sum(line.startswith("candidate_id:") for line in result.output.splitlines()))

    def test_identity_validation_normalizes_isrc_and_rejects_negative_duration(self) -> None:
        identity = PlatformTrackIdentity("identity.normalized", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), duration_ms=0, isrc="us-rc1-76-07839")
        self.assertEqual("USRC17607839", identity.isrc)
        with self.assertRaisesRegex(ValueError, "duration_ms"):
            PlatformTrackIdentity("identity.invalid", IdentityProvider.SPOTIFY, "Synthetic", ("Artist",), duration_ms=-1)

    def test_cli_requires_explicit_migration_then_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "runtime"; database = data_dir / "state" / "app.db"
            database.parent.mkdir(parents=True)
            connection = connect_database(database)
            migrations = load_migrations()
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for migration in migrations[:14]:
                connection.executescript(migration.sql)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, 'now')", (migration.version, migration.name, migration.checksum))
            connection.execute("INSERT INTO canonical_tracks VALUES ('canonical.synthetic.pre15', 'Synthetic', 'now')")
            connection.commit(); connection.close()
            runner = CliRunner()
            args = ["--config", str(CONFIG), "--data-dir", str(data_dir)]
            blocked = runner.invoke(app, ["catalogue", "match", "evidence-status", *args])
            self.assertNotEqual(0, blocked.exit_code)
            self.assertIn("requires migration 15", blocked.output)
            initialized = runner.invoke(app, ["db", "init", *args])
            self.assertEqual(0, initialized.exit_code, initialized.output)
            connection = connect_database(database)
            self.assertEqual(18, apply_migrations(connection))
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=18").fetchone()[0])
            self.assertEqual("Synthetic", connection.execute("SELECT display_title FROM canonical_tracks WHERE canonical_track_id='canonical.synthetic.pre15'").fetchone()[0])
            columns = {row[1] for row in connection.execute("PRAGMA table_info(track_identities)")}
            self.assertTrue({"duration_ms", "isrc"}.issubset(columns))
            store = Phase3Store(connection); store.bind_account("binding.cli", "account.cli", "user.cli")
            connection.close()
            first = runner.invoke(app, ["catalogue", "match", "evidence-status", *args])
            second = runner.invoke(app, ["catalogue", "match", "evidence-status", *args])
            self.assertEqual(0, first.exit_code, first.output)
            self.assertEqual(first.output, second.output)

    def test_backfill_selects_only_ambiguous_link_existing_and_deduplicates_linked_identities(self) -> None:
        self.connection.execute("UPDATE track_identities SET duration_ms=NULL, isrc=NULL")
        self.connection.execute("""INSERT INTO canonical_match_candidates(
            candidate_id, account_binding_id, identity_id, canonical_track_id, action, state,
            confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at
        ) VALUES ('candidate.peer', ?, 'identity.unresolved', NULL, 'create_new', 'ambiguous', .5, '[]', 'synthetic', 'f', 'peer', 'now')""", (self.binding.binding_id,))
        self.connection.execute("""INSERT INTO canonical_match_candidates(
            candidate_id, account_binding_id, identity_id, canonical_track_id, action, state,
            confidence, basis_json, matcher_version, input_fingerprint, candidate_key, created_at
        ) VALUES ('candidate.duplicate', ?, 'identity.unresolved', 'canonical.synthetic', 'link_existing', 'candidate', .5, '[]', 'synthetic', 'f2', 'duplicate', 'now')""", (self.binding.binding_id,))
        self.connection.commit()
        service = MatchEvidenceBackfillService(self.store, self.binding.binding_id)
        plan = service.plan()
        self.assertEqual(3, plan.target_identities)
        self.assertEqual(3, plan.needs_backfill)
        self.assertEqual(0, plan.already_complete)

    def test_backfill_persists_only_selected_evidence_and_is_idempotent_after_missing_isrc(self) -> None:
        self.connection.execute("UPDATE track_identities SET duration_ms=NULL, isrc=NULL")
        self.connection.commit()
        service = MatchEvidenceBackfillService(self.store, self.binding.binding_id)
        candidates_before = self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates").fetchall()
        links_before = self.connection.execute("SELECT link_id, canonical_track_id FROM account_canonical_identity_links").fetchall()
        reader = EvidenceReader({
            "provider.unresolved": SpotifyRecordingEvidence(120000, "us-rc1-76-07839"),
            "provider.linked.a": SpotifyRecordingEvidence(120001, None),
            "provider.linked.b": SpotifyRecordingEvidence(120002, "gbaye6800011"),
        })
        report = service.backfill(reader)
        self.assertEqual(3, report.requested); self.assertEqual(3, report.updated_duration)
        self.assertEqual(2, report.updated_isrc); self.assertEqual(2, report.updated_both)
        self.assertEqual(1, report.remaining_isrc_missing)
        self.assertEqual("USRC17607839", self.connection.execute("SELECT isrc FROM track_identities WHERE identity_id='identity.unresolved'").fetchone()[0])
        rerun_reader = EvidenceReader({})
        rerun = service.backfill(rerun_reader)
        self.assertEqual(0, rerun.requested); self.assertEqual([], rerun_reader.calls)
        self.assertEqual(candidates_before, self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates").fetchall())
        self.assertEqual(links_before, self.connection.execute("SELECT link_id, canonical_track_id FROM account_canonical_identity_links").fetchall())

    def test_backfill_handles_not_found_and_fails_closed_for_account_or_authorization(self) -> None:
        self.connection.execute("UPDATE track_identities SET duration_ms=NULL, isrc=NULL")
        self.connection.commit()
        service = MatchEvidenceBackfillService(self.store, self.binding.binding_id)
        reader = EvidenceReader({
            "provider.unresolved": SpotifyAdapterError(SpotifyErrorKind.NOT_FOUND, "not found"),
            "provider.linked.a": SpotifyRecordingEvidence(1, None),
            "provider.linked.b": SpotifyRecordingEvidence(1, None),
        })
        report = service.backfill(reader)
        self.assertEqual(1, report.provider_missing_or_unavailable)
        with self.assertRaisesRegex(Exception, "does not match"):
            service.require_matching_account("account.other")
        self.connection.execute("UPDATE track_identities SET duration_ms=NULL")
        self.connection.commit()
        denied = EvidenceReader({"provider.linked.a": SpotifyAdapterError(SpotifyErrorKind.NOT_AUTHORIZED, "denied")})
        with self.assertRaises(SpotifyAdapterError):
            service.backfill(denied)

    def test_cli_backfill_renders_aggregate_only_and_requires_no_source_scan(self) -> None:
        self.connection.execute("UPDATE track_identities SET duration_ms=NULL, isrc=NULL")
        self.connection.commit()
        scans_before = self.connection.execute("SELECT COUNT(*) FROM source_scans").fetchone()[0]
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close()
        database.replace(data_dir / "state" / "app.db")
        reader = EvidenceReader({
            "provider.unresolved": SpotifyRecordingEvidence(1, None),
            "provider.linked.a": SpotifyRecordingEvidence(1, None),
            "provider.linked.b": SpotifyRecordingEvidence(1, None),
        })
        runner = CliRunner()
        with patch("spotify_playlist_taxonomy.cli.resolve_client_id", return_value="synthetic"), \
             patch("spotify_playlist_taxonomy.cli._spotify_http_client", return_value=nullcontext(object())), \
             patch("spotify_playlist_taxonomy.cli._spotify_auth_manager", return_value=object()), \
             patch("spotify_playlist_taxonomy.cli.SpotifyReadOnlyAdapter", return_value=reader):
            result = runner.invoke(app, ["catalogue", "match", "evidence-backfill", "--yes", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("target_identities: 3", result.output)
        self.assertIn("updated_duration: 3", result.output)
        self.assertNotIn("provider.unresolved", result.output)
        self.assertNotIn("Synthetic", result.output)
        verification = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(scans_before, verification.execute("SELECT COUNT(*) FROM source_scans").fetchone()[0])
        verification.close()

    def test_cli_backfill_renders_safe_quota_retry_metadata(self) -> None:
        self.connection.execute("UPDATE track_identities SET duration_ms=NULL")
        self.connection.commit()
        database = Path(self.temp.name) / "app.db"
        data_dir = Path(self.temp.name) / "quota-runtime"; (data_dir / "state").mkdir(parents=True)
        self.connection.close()
        database.replace(data_dir / "state" / "app.db")
        reader = EvidenceReader({
            "provider.linked.a": SpotifyAdapterError(
                SpotifyErrorKind.QUOTA_EXHAUSTED, "Spotify API quota is exhausted; read processing stopped",
                reason="QUOTA_EXCEEDED", retry_after_seconds=None,
            ),
        })
        runner = CliRunner()
        with patch("spotify_playlist_taxonomy.cli.resolve_client_id", return_value="synthetic"), \
             patch("spotify_playlist_taxonomy.cli._spotify_http_client", return_value=nullcontext(object())), \
             patch("spotify_playlist_taxonomy.cli._spotify_auth_manager", return_value=object()), \
             patch("spotify_playlist_taxonomy.cli.SpotifyReadOnlyAdapter", return_value=reader):
            result = runner.invoke(app, ["catalogue", "match", "evidence-backfill", "--yes", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("reason: QUOTA_EXCEEDED", result.output)
        self.assertIn("retry_after_seconds: unavailable", result.output)
        self.assertNotIn("provider.linked.a", result.output)


if __name__ == "__main__": unittest.main()
