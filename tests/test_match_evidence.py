from __future__ import annotations
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner
from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.adapters.spotify.reader import SpotifyRecordingEvidence
from spotify_playlist_taxonomy.domain.ingestion import StagedSourceEntry
from spotify_playlist_taxonomy.domain.models import IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database
from spotify_playlist_taxonomy.persistence.database import load_migrations
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.workflows.match_evidence import MatchEvidenceService
from spotify_playlist_taxonomy.workflows.match_evidence_backfill import MatchEvidenceBackfillService
from spotify_playlist_taxonomy.workflows.match_evidence_review import MatchEvidenceReviewService

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

    def test_schema_fifteen_adds_nullable_recording_evidence_to_schema_fourteen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "older.db")
            migrations = load_migrations()
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for migration in migrations[:14]:
                connection.executescript(migration.sql)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, 'now')", (migration.version, migration.name, migration.checksum))
            connection.commit()
            self.assertEqual(15, apply_migrations(connection))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(track_identities)")}
            self.assertTrue({"duration_ms", "isrc"}.issubset(columns))
            connection.close()

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
            self.assertEqual(15, apply_migrations(connection))
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=15").fetchone()[0])
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
