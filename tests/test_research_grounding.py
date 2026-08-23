from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from typer.testing import CliRunner

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import load_research_policy_document, load_taxonomy_document
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.domain.research import EvidenceType
from spotify_playlist_taxonomy.persistence import ResearchStore, SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.persistence.identity_external_evidence import IdentityExternalEntityEvidence, IdentityExternalEvidenceStore
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.ports.research import ProviderDescriptor, ProviderOutcomeStatus, ResearchProvider, ResearchRequest, ResearchResult
from spotify_playlist_taxonomy.workflows.research import ResearchCoordinator
from spotify_playlist_taxonomy.workflows.research_grounding import CanonicalExternalSubjectResolver, ResearchGroundingPreviewService


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 2, tzinfo=UTC)
MBID_A = "11111111-2222-3333-4444-555555555555"
MBID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class CaptureProvider(ResearchProvider):
    def __init__(self) -> None:
        self.requests: list[ResearchRequest] = []
        self._descriptor = ProviderDescriptor("musicbrainz", "synthetic.v1", "research-provider.v1", (EvidenceType.RECORDING_IDENTITY,), False, 0)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def research(self, request: ResearchRequest) -> ResearchResult:
        self.requests.append(request)
        return ResearchResult(request.request_id, "musicbrainz", "synthetic.v1", "query.synthetic", "q" * 64, NOW.isoformat().replace("+00:00", "Z"), ProviderOutcomeStatus.NO_RESULT)


class ResearchGroundingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        sync_taxonomy(self.connection, load_taxonomy_document(ROOT / "config" / "taxonomy.yaml"))
        self.store = SQLiteStateStore(self.connection)
        self.binding, self.source, self.scan = "binding.synthetic.grounding", "source.synthetic.grounding", "scan.synthetic.grounding"
        self.connection.execute("INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic.grounding', 'user.synthetic.grounding', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)", (self.binding,))
        phase3 = Phase3Store(self.connection)
        phase3.register_liked_source(self.source, self.binding)
        phase3.start_scan(scan_id=self.scan, source_id=self.source, binding_id=self.binding, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _canonical(self, suffix: str) -> str:
        canonical_id = f"canonical.synthetic.grounding.{suffix}"
        self.store.save_canonical_track(CanonicalTrack(canonical_id, "Synthetic", ("Invented",)))
        return canonical_id

    def _link(self, canonical_id: str, suffix: str) -> str:
        identity_id = f"identity.synthetic.grounding.{suffix}"
        self.store.save_track_identity(PlatformTrackIdentity(identity_id, IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), f"tracksynthetic{suffix}"))
        self.connection.execute("INSERT INTO account_track_identity_state VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, '2026-01-01T00:00:00Z')", (self.binding, identity_id, self.scan, self.source))
        self.connection.execute("INSERT INTO account_canonical_identity_links VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')", (f"link.synthetic.grounding.{suffix}", self.binding, identity_id, canonical_id))
        self.connection.commit()
        return identity_id

    def _evidence(self, identity_id: str, state: str = "resolved", recording_id: str | None = MBID_A, *, fresh: bool = True, provider: str = "musicbrainz", entity_type: str = "recording") -> None:
        stamp = NOW.isoformat().replace("+00:00", "Z")
        until = (NOW + timedelta(days=1) if fresh else NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        IdentityExternalEvidenceStore(self.connection).upsert(IdentityExternalEntityEvidence(
            f"external.synthetic.grounding.{identity_id}", identity_id, provider, entity_type,
            recording_id if state == "resolved" else None, state, "isrc", "synthetic-query", "synthetic.v1", stamp, until, None, None,
        ))

    def _resolver(self) -> CanonicalExternalSubjectResolver:
        return CanonicalExternalSubjectResolver(self.connection, now=lambda: NOW)

    def test_unanimity_and_all_failure_states_fail_closed(self) -> None:
        cases = (
            ("single", (("resolved", MBID_A, True),), "exact"),
            ("same", (("resolved", MBID_A, True), ("resolved", MBID_A, True)), "exact"),
            ("different", (("resolved", MBID_A, True), ("resolved", MBID_B, True)), "resolved_disagreement"),
            ("ambiguous", (("resolved", MBID_A, True), ("ambiguous", None, True)), "nonresolved_external_evidence"),
            ("not-found", (("resolved", MBID_A, True), ("not_found", None, True)), "nonresolved_external_evidence"),
            ("failed", (("resolved", MBID_A, True), ("lookup_failed", None, True)), "nonresolved_external_evidence"),
            ("stale", (("resolved", MBID_A, True), ("resolved", MBID_A, False)), "stale_external_evidence"),
            ("missing", (("resolved", MBID_A, True),), "missing_external_evidence"),
        )
        for suffix, values, expected in cases:
            with self.subTest(suffix=suffix):
                canonical_id = self._canonical(suffix)
                identities = [self._link(canonical_id, f"{suffix}.{index}") for index in range(len(values) + (1 if suffix == "missing" else 0))]
                for identity_id, (state, recording_id, fresh) in zip(identities, values):
                    self._evidence(identity_id, state, recording_id, fresh=fresh)
                item = self._resolver().resolve(canonical_id)
                self.assertEqual(expected, item.state if expected == "exact" else item.reason)

    def test_invalid_wrong_kind_legacy_and_missing_context_are_unavailable(self) -> None:
        invalid = self._canonical("invalid"); invalid_id = self._link(invalid, "invalid")
        self.connection.execute("INSERT INTO identity_external_entity_evidence VALUES (?, ?, 'musicbrainz', 'recording', 'not-a-mbid', 'resolved', 'isrc', 'query', 'synthetic.v1', ?, ?, NULL, NULL)", ("external.synthetic.invalid", invalid_id, NOW.isoformat().replace("+00:00", "Z"), (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z")))
        self.connection.commit()
        wrong = self._canonical("wrong"); wrong_id = self._link(wrong, "wrong"); self._evidence(wrong_id, provider="wikidata")
        wrong_entity = self._canonical("wrong-entity"); wrong_entity_id = self._link(wrong_entity, "wrong-entity"); self._evidence(wrong_entity_id, entity_type="work")
        legacy = self._canonical("legacy")
        self.store.save_track_identity(PlatformTrackIdentity("identity.synthetic.grounding.legacy", IdentityProvider.SPOTIFY, "Synthetic", ("Invented",), "tracksyntheticlegacy"))
        self.store.save_identity_link(__import__("spotify_playlist_taxonomy.domain.models", fromlist=["CanonicalTrackIdentityLink"]).CanonicalTrackIdentityLink("link.synthetic.grounding.legacy", legacy, "identity.synthetic.grounding.legacy"))
        self.assertEqual("invalid_recording_id", self._resolver().resolve(invalid).reason)
        self.assertEqual("missing_external_evidence", self._resolver().resolve(wrong).reason)
        self.assertEqual("missing_external_evidence", self._resolver().resolve(wrong_entity).reason)
        self.assertEqual("no_current_spotify_identity", self._resolver().resolve(legacy).reason)
        self.connection.execute("UPDATE platform_account_bindings SET active=0 WHERE binding_id=?", (self.binding,)); self.connection.commit()
        self.assertEqual("no_active_account_context", self._resolver().resolve(invalid).reason)

    def test_primary_failure_reason_uses_documented_precedence(self) -> None:
        canonical_id = self._canonical("precedence")
        stale = self._link(canonical_id, "precedence.stale")
        self._evidence(stale, fresh=False)
        self._link(canonical_id, "precedence.missing")
        self.assertEqual("missing_external_evidence", self._resolver().resolve(canonical_id).reason)

    def test_preview_is_deterministic_reconciled_bounded_and_read_only(self) -> None:
        exact = self._canonical("a.exact"); exact_id = self._link(exact, "a.exact"); self._evidence(exact_id)
        missing = self._canonical("b.missing"); self._link(missing, "b.missing")
        before = tuple(self.connection.iterdump())
        service = ResearchGroundingPreviewService(self._resolver())
        report = service.preview(1)
        self.assertEqual(2, report.canonical_tracks_total)
        self.assertEqual(1, report.grounding_exact_canonicals)
        self.assertEqual(1, report.missing_external_evidence_canonicals)
        self.assertEqual(report.canonical_tracks_total, report.grounding_exact_canonicals + report.grounding_unavailable_canonicals)
        self.assertEqual((exact,), tuple(item.canonical_track_id for item in report.selected))
        self.assertEqual(before, tuple(self.connection.iterdump()))
        with self.assertRaises(ValueError): service.preview(0)
        with self.assertRaises(ValueError): service.preview(26)

    def test_safe_grounding_is_the_existing_direct_musicbrainz_identifier_form(self) -> None:
        canonical_id = self._canonical("coordinator"); identity_id = self._link(canonical_id, "coordinator"); self._evidence(identity_id)
        provider = CaptureProvider()
        coordinator = ResearchCoordinator(
            store=ResearchStore(self.connection), providers=[provider],
            policy=load_research_policy_document(ROOT / "config" / "research.yaml"),
            phase4_bundle_digest="a" * 64, now=lambda: NOW, sleep=lambda _: None,
            subject_resolver=self._resolver(),
        )
        coordinator.run((canonical_id,), provider_id="musicbrainz")
        self.assertIn(("musicbrainz_recording", MBID_A), {(item.namespace, item.value) for item in provider.requests[0].independent_identifiers})

    def test_preview_cli_is_offline_and_hides_recording_identifier(self) -> None:
        data_dir = Path(self.temp.name) / "runtime"; (data_dir / "state").mkdir(parents=True)
        path = data_dir / "state" / "app.db"
        target = connect_database(path); self.connection.backup(target); target.close()
        result = CliRunner().invoke(app, ["research", "grounding", "preview", "--limit", "1", "--config", str(ROOT / "config" / "application.yaml"), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("network_access: no", result.output)
        self.assertIn("database_write: no", result.output)
        self.assertNotIn(MBID_A, result.output)


if __name__ == "__main__":
    unittest.main()
