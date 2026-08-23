from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.adapters.research.musicbrainz import MusicBrainzRecordingLookupResult
from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import load_phase4_bundle, load_taxonomy_document
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.domain.research import EvidenceType
from spotify_playlist_taxonomy.persistence import ResearchStore, SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.persistence.identity_external_evidence import IdentityExternalEntityEvidence, IdentityExternalEvidenceStore
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.ports.research import ProviderDescriptor
from spotify_playlist_taxonomy.workflows.classification_readiness import ClassificationReadinessService
from spotify_playlist_taxonomy.workflows.research import EvidenceSufficiencyService, ResearchActionabilityService
from spotify_playlist_taxonomy.workflows.research_grounding import CanonicalExternalSubjectResolver
from spotify_playlist_taxonomy.workflows.research_grounding_acquisition import ResearchGroundingAcquisitionService


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 2, tzinfo=UTC)
MBID = "11111111-2222-3333-4444-555555555555"


class ResearchGroundingAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        sync_taxonomy(self.connection, load_taxonomy_document(ROOT / "config" / "taxonomy.yaml"))
        self.bundle = load_phase4_bundle(ROOT / "config" / "application.yaml")
        self.store = SQLiteStateStore(self.connection)
        self.binding, self.source, self.scan = "binding.synthetic.acquire", "source.synthetic.acquire", "scan.synthetic.acquire"
        self.connection.execute("INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic.acquire', 'user.synthetic.acquire', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)", (self.binding,))
        phase3 = Phase3Store(self.connection)
        phase3.register_liked_source(self.source, self.binding)
        phase3.start_scan(scan_id=self.scan, source_id=self.source, binding_id=self.binding, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")
        descriptors = (ProviderDescriptor("synthetic", "synthetic.v1", "synthetic.v1", tuple(EvidenceType)),)
        sufficiency = EvidenceSufficiencyService(store=ResearchStore(self.connection), descriptors=descriptors, phase4_bundle_digest=self.bundle.digest)
        self.readiness = ClassificationReadinessService(self.connection, self.bundle, ResearchActionabilityService(sufficiency, now=lambda: NOW))
        self.service = ResearchGroundingAcquisitionService(
            self.connection, self.readiness, CanonicalExternalSubjectResolver(self.connection, now=lambda: NOW),
            positive_ttl_hours=24, negative_ttl_hours=1, ambiguous_ttl_hours=1, now=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _canonical(self, suffix: str, isrc: str | None = "USRC17607839") -> tuple[str, str]:
        canonical_id = f"canonical.synthetic.acquire.{suffix}"
        identity_id = f"identity.synthetic.acquire.{suffix}"
        self.store.save_canonical_track(CanonicalTrack(canonical_id, "Synthetic", ("Invented",)))
        provider_track_id = "tracksynthetic" + re.sub(r"[^A-Za-z0-9]", "", suffix)
        self.store.save_track_identity(PlatformTrackIdentity(identity_id, IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), provider_track_id))
        self.connection.execute("UPDATE track_identities SET isrc=? WHERE identity_id=?", (isrc, identity_id))
        self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, identity_id, self.scan, self.source))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')", (f"link.synthetic.acquire.{suffix}", self.binding, identity_id, canonical_id))
        self.connection.commit()
        return canonical_id, identity_id

    def _evidence(self, identity_id: str, state: str, *, fresh: bool = True) -> None:
        stamp = NOW.isoformat().replace("+00:00", "Z")
        expiry = NOW + timedelta(days=1) if fresh else NOW - timedelta(seconds=1)
        IdentityExternalEvidenceStore(self.connection).upsert(IdentityExternalEntityEvidence(
            f"external.synthetic.acquire.{identity_id}", identity_id, "musicbrainz", "recording",
            MBID if state == "resolved" else None, state, "isrc", "synthetic", "synthetic.v1",
            stamp, expiry.isoformat().replace("+00:00", "Z"), None, None,
        ))

    def test_preview_is_deterministic_read_only_and_uses_existing_due_semantics(self) -> None:
        eligible, _ = self._canonical("a.eligible")
        exact, exact_identity = self._canonical("b.exact"); self._evidence(exact_identity, "resolved")
        missing, _ = self._canonical("c.missing", None)
        invalid, _ = self._canonical("d.invalid", "INVALID")
        ambiguous, ambiguous_identity = self._canonical("e.ambiguous"); self._evidence(ambiguous_identity, "ambiguous")
        stale, stale_identity = self._canonical("f.stale"); self._evidence(stale_identity, "not_found", fresh=False)
        before = tuple(self.connection.iterdump())
        preview = self.service.preview()
        self.assertEqual((6, 1, 2, 4), (
            preview.research_needed_canonicals, preview.grounding_exact_canonicals,
            preview.grounding_acquisition_eligible_canonicals, preview.grounding_acquisition_unavailable_canonicals,
        ))
        self.assertEqual((eligible, stale), tuple(item.canonical_track_id for item in self.service.plan(25)))
        self.assertEqual({"already_exact_grounded": 1, "missing_isrc": 1, "invalid_isrc": 1, "external_evidence_not_due": 1}, dict(preview.unavailable_reason_counts))
        self.assertEqual(before, tuple(self.connection.iterdump()))
        with self.assertRaises(ValueError): self.service.plan(0)
        with self.assertRaises(ValueError): self.service.plan(26)

    def test_fetch_reuses_identity_primitive_and_creates_exact_grounding_only(self) -> None:
        canonical_id, identity_id = self._canonical("fetch")
        candidates_before = tuple(self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id"))
        class Lookup:
            calls: list[str] = []
            def lookup_recordings_by_isrc(self, value: str) -> MusicBrainzRecordingLookupResult:
                self.calls.append(value)
                return MusicBrainzRecordingLookupResult(value, (MBID,), "synthetic", "synthetic.v1", NOW.isoformat().replace("+00:00", "Z"))
        report = self.service.fetch(self.service.plan(1), Lookup())
        self.assertEqual((1, 1, 1, 0, 0, 0, 0, 0), (
            report.selected, report.processed, report.resolved, report.ambiguous, report.not_found,
            report.insufficient_input, report.lookup_failed, report.skipped_stale_or_ineligible,
        ))
        self.assertEqual((canonical_id,), report.exactly_grounded_canonical_ids)
        self.assertTrue(CanonicalExternalSubjectResolver(self.connection, now=lambda: NOW).resolve(canonical_id).is_exact)
        self.assertEqual(candidates_before, tuple(self.connection.execute("SELECT candidate_id, state FROM canonical_match_candidates ORDER BY candidate_id")))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM research_provider_attempts").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM phase6_characteristic_derivations").fetchone()[0])
        self.assertEqual(identity_id, IdentityExternalEvidenceStore(self.connection).for_identities((identity_id,))[0].identity_id)

    def test_fetch_rechecks_selected_target_before_provider_work(self) -> None:
        _, identity_id = self._canonical("recheck")
        selected = self.service.plan(1)
        self._evidence(identity_id, "ambiguous")
        class Lookup:
            def lookup_recordings_by_isrc(self, _: str):
                raise AssertionError("ineligible target must not call provider")
        report = self.service.fetch(selected, Lookup())
        self.assertEqual((1, 0, 1), (report.selected, report.processed, report.skipped_stale_or_ineligible))

    def test_cli_preview_is_offline_and_fetch_requires_network_flag(self) -> None:
        canonical_id, _ = self._canonical("cli")
        data_dir = Path(self.temp.name) / "runtime"; (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db"); self.connection.backup(target); target.close()
        args = ["research", "grounding", "acquire", "preview", "--limit", "1", "--config", str(ROOT / "config" / "application.yaml"), "--data-dir", str(data_dir)]
        first = CliRunner().invoke(app, args)
        second = CliRunner().invoke(app, args)
        self.assertEqual(0, first.exit_code, first.output)
        self.assertEqual(first.output, second.output)
        self.assertIn(f"canonical_track_id: {canonical_id}", first.output)
        self.assertIn("network_access: no", first.output)
        self.assertIn("database_write: no", first.output)
        blocked = CliRunner().invoke(app, ["research", "grounding", "acquire", "fetch", "--limit", "1", "--yes", "--config", str(ROOT / "config" / "application.yaml"), "--data-dir", str(data_dir)])
        self.assertNotEqual(0, blocked.exit_code)
        self.assertIn("requires explicit --network", blocked.output)

    def test_cli_eligible_scope_renders_the_same_batch_as_fetch_plan(self) -> None:
        unavailable, _ = self._canonical("a.missing", None)
        first_eligible, _ = self._canonical("b.eligible")
        second_eligible, _ = self._canonical("c.eligible")
        self.assertEqual((first_eligible, second_eligible), tuple(item.canonical_track_id for item in self.service.plan(25)))
        data_dir = Path(self.temp.name) / "runtime-eligible"; (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db"); self.connection.backup(target); target.close()
        base = ["research", "grounding", "acquire", "preview", "--config", str(ROOT / "config" / "application.yaml"), "--data-dir", str(data_dir)]
        all_result = CliRunner().invoke(app, [*base, "--limit", "1"])
        eligible_one = CliRunner().invoke(app, [*base, "--scope", "eligible", "--limit", "1"])
        eligible_all = CliRunner().invoke(app, [*base, "--scope", "eligible", "--limit", "25"])
        self.assertEqual(0, all_result.exit_code, all_result.output)
        self.assertEqual(0, eligible_one.exit_code, eligible_one.output)
        self.assertEqual(0, eligible_all.exit_code, eligible_all.output)
        self.assertIn(f"canonical_track_id: {unavailable}", all_result.output)
        self.assertNotIn(unavailable, eligible_one.output)
        self.assertIn(f"canonical_track_id: {first_eligible}\tacquisition=eligible", eligible_one.output)
        self.assertIn(f"canonical_track_id: {first_eligible}\tacquisition=eligible", eligible_all.output)
        self.assertIn(f"canonical_track_id: {second_eligible}\tacquisition=eligible", eligible_all.output)
        fields = ("research_needed_canonicals", "grounding_exact_canonicals", "grounding_acquisition_eligible_canonicals", "grounding_acquisition_unavailable_canonicals")
        for field in fields:
            self.assertEqual(
                next(line for line in all_result.output.splitlines() if line.startswith(field + ":")),
                next(line for line in eligible_one.output.splitlines() if line.startswith(field + ":")),
            )
        self.assertNotIn(MBID, eligible_all.output)
        invalid = CliRunner().invoke(app, [*base, "--scope", "invalid", "--limit", "1"])
        self.assertNotEqual(0, invalid.exit_code)

    def test_cli_fetch_uses_mocked_existing_lookup_and_reports_exact_grounding(self) -> None:
        canonical_id, _ = self._canonical("cli-fetch")
        data_dir = Path(self.temp.name) / "runtime-fetch"; (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db"); self.connection.backup(target); target.close()
        class FakeProvider:
            def __init__(self, **_: object) -> None:
                pass
            @property
            def descriptor(self) -> ProviderDescriptor:
                return ProviderDescriptor("musicbrainz", "synthetic.v1", "research-provider.v1", tuple(EvidenceType))
            def lookup_recordings_by_isrc(self, value: str) -> MusicBrainzRecordingLookupResult:
                return MusicBrainzRecordingLookupResult(value, (MBID,), "synthetic", "synthetic.v1", datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        args = ["research", "grounding", "acquire", "fetch", "--network", "--yes", "--limit", "1", "--config", str(ROOT / "config" / "application.yaml"), "--data-dir", str(data_dir)]
        with patch("spotify_playlist_taxonomy.cli.MusicBrainzResearchProvider", FakeProvider):
            result = CliRunner().invoke(app, args)
        self.assertEqual(0, result.exit_code, f"{result.output}\n{result.exception!r}")
        self.assertIn("resolved: 1", result.output)
        self.assertIn(f"exactly_grounded_canonical_id: {canonical_id}", result.output)
        self.assertIn("matching_mutations: 0", result.output)
        verification = connect_database(data_dir / "state" / "app.db")
        self.assertEqual("resolved", IdentityExternalEvidenceStore(verification).for_identities((f"identity.synthetic.acquire.cli-fetch",))[0].lookup_state)
        self.assertEqual(0, verification.execute("SELECT COUNT(*) FROM research_provider_attempts").fetchone()[0])
        verification.close()


if __name__ == "__main__":
    unittest.main()
