from __future__ import annotations

import json
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa:F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack, CanonicalTrackIdentityLink, EvidenceKind, IdentityProvider,
    PlatformTrackIdentity, ResearchEvidence, ResearchSource,
)
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.workflows.classification_readiness import (
    ClassificationReadinessService, ReadinessBucket,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"


class ClassificationReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        self.bundle = load_phase4_bundle(CONFIG)
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.store = SQLiteStateStore(self.connection)
        self.binding, self.source, self.scan = "binding.synthetic.readiness", "source.synthetic.readiness", "scan.synthetic.readiness"
        self.connection.execute("INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic.readiness', 'user.synthetic.readiness', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)", (self.binding,))
        phase3 = Phase3Store(self.connection); phase3.register_liked_source(self.source, self.binding)
        phase3.start_scan(scan_id=self.scan, source_id=self.source, binding_id=self.binding, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")
        self.service = ClassificationReadinessService(self.connection, self.bundle)

    def tearDown(self) -> None:
        self.connection.close(); self.temp.cleanup()

    def _canonical(self, suffix: str, *, mapping: bool = False) -> str:
        canonical_id = f"canonical.synthetic.readiness.{suffix}"
        self.store.save_canonical_track(CanonicalTrack(canonical_id, f"Synthetic {suffix}", ("Invented",)))
        if mapping:
            identity_id = f"identity.synthetic.readiness.{suffix}"
            self.store.save_track_identity(PlatformTrackIdentity(identity_id, IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), f"tracksynthetic{suffix.replace('_', '')}"))
            self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, identity_id, self.scan, self.source))
            self.connection.execute("INSERT INTO account_canonical_identity_links VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')", (f"link.synthetic.readiness.{suffix}", self.binding, identity_id, canonical_id))
            self.connection.commit()
        return canonical_id

    def _account_link(self, canonical_id: str, suffix: str, provider: IdentityProvider = IdentityProvider.SPOTIFY) -> None:
        identity_id = f"identity.synthetic.readiness.{suffix}"
        self.store.save_track_identity(PlatformTrackIdentity(identity_id, provider, "Synthetic", ("Invented",), f"tracksynthetic{suffix}"))
        self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, identity_id, self.scan, self.source))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')", (f"link.synthetic.readiness.{suffix}", self.binding, identity_id, canonical_id))
        self.connection.commit()

    def _evidence(self, canonical_id: str) -> None:
        self.store.save_evidence(ResearchEvidence(
            f"evidence.synthetic.readiness.{canonical_id.rsplit('.', 1)[-1]}", canonical_id,
            ResearchSource.WIKIDATA, EvidenceKind.SOURCED_FACT,
            json.dumps({"claim": "style_tradition", "value": "pop"}), "synthetic://evidence", .9,
            provider_id="wikidata", provider_version="synthetic.v1", request_id="request.synthetic",
            query_id="query.synthetic", subject_fingerprint="a" * 64, subject_scope="recording",
            subject_match="exact", evidence_type="style_tradition", claim_key="style_tradition",
            quality_band="high", normalized_schema_version="research-evidence.v1",
            normalizer_version="evidence-normalizer.v1", retrieved_at="2026-01-01T00:00:00Z",
            fresh_until="2099-01-01T00:00:00Z", content_digest="b" * 64,
        ))

    def _proposal(self, canonical_id: str, semantic_id: str, decision: str = "include", state: str = "supported", tag: str = "") -> None:
        spec = next(item for item in self.bundle.specifications if item.logical_playlist_id == semantic_id)
        self.connection.execute(
            "INSERT INTO phase6_proposals VALUES (?, ?, ?, ?, ?, .9, ?, ?, ?, 'synthetic.v1', ?, ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')",
            (f"proposal.synthetic.readiness.{canonical_id.rsplit('.', 1)[-1]}.{semantic_id}{tag}", canonical_id, semantic_id, decision, state,
             spec.specification_version, self.bundle.taxonomy.taxonomy_version,
             self.bundle.vocabulary.vocabulary_version, self.bundle.digest, f"input.{semantic_id}{tag}"),
        )
        self.connection.commit()

    def _characteristic(self, canonical_id: str, name: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO phase6_characteristic_derivations VALUES (?, ?, ?, ?, 'enum', 'deterministic_rule', .9, 'synthetic.v1', ?, ?, ?, 'current', 'synthetic', '2026-01-01T00:00:00Z')",
            (f"derivation.synthetic.readiness.{canonical_id.rsplit('.', 1)[-1]}.{name}", canonical_id, name, json.dumps(value),
             self.bundle.vocabulary.vocabulary_version, self.bundle.digest, f"input.{name}"),
        )
        self.connection.commit()

    def test_aggregate_buckets_distribution_and_deterministic_selection(self) -> None:
        self._canonical("no_mapping")
        research = self._canonical("research", mapping=True)
        local = self._canonical("local", mapping=True); self._evidence(local)
        ready = self._canonical("ready", mapping=True); self._proposal(ready, "vibe.chill"); self._proposal(ready, "activity.driving")
        greek = self._canonical("greek", mapping=True); self._proposal(greek, "genre.pop"); self._characteristic(greek, "vocals_lyrics.lyrical_language_profile", "greek")
        unresolved = self._canonical("unresolved", mapping=True); self._proposal(unresolved, "genre.pop")
        blocked = self._canonical("blocked", mapping=True); self._proposal(blocked, "vibe.chill", state="conflicting")

        before = self.connection.total_changes
        first, second = self.service.report(25), self.service.report(25)
        self.assertEqual(first, second); self.assertEqual(before, self.connection.total_changes)
        self.assertEqual(7, first.classification_eligible_canonicals)
        self.assertEqual(6, first.spotify_population_eligible_canonicals)
        self.assertEqual(1, first.qualifying_research_evidence_canonicals)
        self.assertEqual(2, first.population_ready_canonicals)
        self.assertEqual(1, first.local_classification_ready_canonicals)
        self.assertEqual(2, first.research_needed_canonicals)
        self.assertEqual(1, first.review_blocked_canonicals)
        self.assertEqual(1, first.population_mapping_blocked_canonicals)
        self.assertEqual(1, first.genre_routing_unresolved_canonicals)
        self.assertEqual("population", first.selected_action)
        self.assertEqual(tuple(sorted((ready, greek))), tuple(item.canonical_track_id for item in first.selections[:2]))
        self.assertEqual(ReadinessBucket.POPULATION_READY, first.selections[0].bucket)
        distribution = dict(first.target_distribution)
        self.assertEqual(51, len(distribution)); self.assertEqual(1, distribution["output.vibe.chill"])
        self.assertEqual(1, distribution["output.activity.driving"]); self.assertEqual(1, distribution["output.genre.pop.greek"])
        self.assertTrue(any(value == 0 for value in distribution.values()))

    def test_selection_falls_through_local_then_research_and_limit_is_bounded(self) -> None:
        local_b = self._canonical("local_b", mapping=True); self._evidence(local_b)
        local_a = self._canonical("local_a", mapping=True); self._evidence(local_a)
        report = self.service.report(1)
        self.assertEqual("local-classification", report.selected_action)
        self.assertEqual((local_a,), tuple(item.canonical_track_id for item in report.selections))
        self.assertEqual("derive_then_classify", report.selections[0].next_action)
        self.connection.execute("DELETE FROM research_evidence"); self.connection.commit()
        report = self.service.report(25)
        self.assertEqual("research", report.selected_action)
        self.assertTrue(all(item.next_action == "research_then_derive_then_classify" for item in report.selections))
        with self.assertRaises(ValueError): self.service.report(0)
        with self.assertRaises(ValueError): self.service.report(26)

    def test_nonmembership_and_mapping_boundaries_fail_closed(self) -> None:
        excluded = self._canonical("excluded", mapping=True); self._proposal(excluded, "vibe.chill", "exclude")
        abstained = self._canonical("abstained", mapping=True); self._proposal(abstained, "vibe.chill", "abstain", "insufficient")
        foreign = self._canonical("foreign", mapping=True); self._proposal(foreign, "genre.pop")
        self._characteristic(foreign, "vocals_lyrics.lyrical_language_profile", "non_greek")
        duplicate = self._canonical("duplicate", mapping=True); self._proposal(duplicate, "vibe.chill"); self._proposal(duplicate, "vibe.chill", tag=".two")
        multiple = self._canonical("multiple", mapping=True)
        self.store.save_track_identity(PlatformTrackIdentity("identity.synthetic.readiness.multiple.two", IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), "tracksyntheticmultipletwo"))
        self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, 'identity.synthetic.readiness.multiple.two', ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, self.scan, self.source))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES ('link.synthetic.readiness.multiple.two', ?, 'identity.synthetic.readiness.multiple.two', ?, '2026-01-01T00:00:00Z')", (self.binding, multiple)); self.connection.commit()
        report = self.service.report(25)
        distribution = dict(report.target_distribution)
        self.assertEqual(1, report.effective_exclude_canonicals)
        self.assertEqual(1, report.effective_abstain_canonicals)
        self.assertEqual(1, distribution["output.genre.pop.foreign"])
        self.assertEqual(1, distribution["output.vibe.chill"])
        self.assertEqual(1, report.population_mapping_blocked_canonicals)
        self.assertEqual("population", report.selected_action)

    def test_mapping_diagnostics_are_exclusive_and_use_active_account_context(self) -> None:
        self._canonical("zero")
        legacy = self._canonical("legacy")
        self.store.save_track_identity(PlatformTrackIdentity("identity.synthetic.readiness.legacy", IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), "tracksyntheticlegacy"))
        self.store.save_identity_link(CanonicalTrackIdentityLink("link.synthetic.readiness.legacy", legacy, "identity.synthetic.readiness.legacy"))
        safe = self._canonical("safe", mapping=True)
        multiple = self._canonical("multiple_reason", mapping=True); self._account_link(multiple, "multiple_reason_two")
        mismatch = self._canonical("mismatch"); self._account_link(mismatch, "mismatch_musicbrainz", IdentityProvider.MUSICBRAINZ)
        report = self.service.report(25)
        self.assertEqual(1, report.spotify_mapping_exactly_one_safe_canonicals)
        self.assertEqual(2, report.spotify_mapping_zero_current_canonicals)
        self.assertEqual(1, report.spotify_mapping_multiple_current_canonicals)
        self.assertEqual(1, report.spotify_mapping_provider_mismatch_canonicals)
        self.assertEqual(0, report.spotify_mapping_account_context_unavailable_canonicals)
        self.assertEqual(5, report.spotify_mapping_exactly_one_safe_canonicals + report.spotify_mapping_zero_current_canonicals + report.spotify_mapping_multiple_current_canonicals + report.spotify_mapping_provider_mismatch_canonicals + report.spotify_mapping_invalid_canonicals)
        self.assertEqual(2, report.canonicals_with_current_spotify_link)
        self.connection.execute("UPDATE platform_account_bindings SET active=0 WHERE binding_id=?", (self.binding,)); self.connection.commit()
        offline = self.service.report(25)
        self.assertEqual(5, offline.spotify_mapping_account_context_unavailable_canonicals)
        self.assertEqual(0, offline.spotify_population_eligible_canonicals)

    def test_cli_is_read_only_offline_and_renders_selected_population_targets(self) -> None:
        data_dir = Path(self.temp.name) / "runtime"
        runner = CliRunner()
        self.connection.close()
        result = runner.invoke(app, ["db", "init", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        connection = connect_database(data_dir / "state" / "app.db")
        store = SQLiteStateStore(connection)
        binding, source, scan = "binding.synthetic.cli", "source.synthetic.cli", "scan.synthetic.cli"
        connection.execute("INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic.cli', 'user.synthetic.cli', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)", (binding,))
        phase3 = Phase3Store(connection); phase3.register_liked_source(source, binding)
        phase3.start_scan(scan_id=scan, source_id=source, binding_id=binding, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")
        canonical = "canonical.synthetic.cli.ready"
        store.save_canonical_track(CanonicalTrack(canonical, "Synthetic", ("Invented",)))
        store.save_track_identity(PlatformTrackIdentity("identity.synthetic.cli.ready", IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), "tracksyntheticcli"))
        connection.execute("INSERT INTO account_track_identity_state VALUES (?, 'identity.synthetic.cli.ready', ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (binding, scan, source))
        connection.execute("INSERT INTO account_canonical_identity_links VALUES ('link.synthetic.cli.ready', ?, 'identity.synthetic.cli.ready', ?, '2026-01-01T00:00:00Z')", (binding, canonical))
        spec = next(item for item in self.bundle.specifications if item.logical_playlist_id == "vibe.chill")
        connection.execute("INSERT INTO phase6_proposals VALUES ('proposal.synthetic.cli.ready', ?, 'vibe.chill', 'include', 'supported', .9, ?, ?, ?, 'synthetic.v1', 'input', ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')", (canonical, spec.specification_version, self.bundle.taxonomy.taxonomy_version, self.bundle.vocabulary.vocabulary_version, self.bundle.digest)); connection.commit()
        before = tuple(connection.iterdump()); connection.close(); del connection; gc.collect()
        with patch("socket.socket.connect", side_effect=AssertionError("network attempted")):
            result = runner.invoke(app, ["catalogue", "classify", "readiness", "--limit", "1", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("selected_action: population", result.output)
        self.assertIn("selected_desired_target_ids: output.vibe.chill", result.output)
        connection = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(before, tuple(connection.iterdump())); connection.close(); del connection; gc.collect()


if __name__ == "__main__":
    unittest.main()
