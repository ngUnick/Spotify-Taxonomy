from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.config import load_research_policy_document, load_taxonomy_document
from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack, EvidenceKind, ResearchEvidence, ResearchSource,
)
from spotify_playlist_taxonomy.domain.research import (
    EvidenceType, QualityBand, SubjectMatch, SubjectScope, SufficiencyState,
)
from spotify_playlist_taxonomy.persistence import (
    ResearchStore, SQLiteStateStore, apply_migrations, assert_integrity,
    connect_database, current_schema_version, sync_taxonomy,
)
from spotify_playlist_taxonomy.persistence.database import load_migrations, utc_now
from spotify_playlist_taxonomy.ports.research import (
    ProviderAssertion, ProviderDescriptor, ProviderOutcomeStatus,
    ResearchProvider, ResearchRequest, ResearchResult,
)
from spotify_playlist_taxonomy.workflows.research import (
    EvidenceSufficiencyService, ResearchCoordinator, subject_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def iso(value: datetime = NOW) -> str:
    return value.isoformat().replace("+00:00", "Z")


class FakeProvider(ResearchProvider):
    def __init__(self, provider_id: str, outcomes: list[ProviderOutcomeStatus], *,
                 value: str = "rock", adapter_version: str | None = None):
        source = ResearchSource.MUSICBRAINZ if provider_id == "musicbrainz" else ResearchSource.WIKIDATA
        self.source = source
        self.outcomes = list(outcomes)
        self.value = value
        self.calls = 0
        self.requests: list[ResearchRequest] = []
        self._descriptor = ProviderDescriptor(
            provider_id, adapter_version or f"{provider_id}.v1", "research-provider.v1",
            tuple(EvidenceType), False, 0,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def research(self, request: ResearchRequest) -> ResearchResult:
        self.calls += 1
        self.requests.append(request)
        status = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        assertions = ()
        if status is ProviderOutcomeStatus.SUCCESS:
            assertions = (ProviderAssertion(
                "style", EvidenceType.STYLE_TRADITION,
                {"claim": "style_tradition", "value": self.value},
                EvidenceKind.SOURCED_FACT, self.source,
                f"https://example.invalid/{self.descriptor.provider_id}/statement",
                SubjectScope.RECORDING, SubjectMatch.EXACT,
                quality_band=QualityBand.HIGH,
            ),)
        return ResearchResult(
            request.request_id, self.descriptor.provider_id,
            self.descriptor.adapter_version, f"query.{self.calls}",
            f"fingerprint.{self.calls}", iso(), status, assertions,
            retry_after_seconds=0 if status is ProviderOutcomeStatus.TRANSIENT_ERROR else None,
            error_category="temporary" if status is ProviderOutcomeStatus.TRANSIENT_ERROR else None,
        )


class Phase5ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "app.db")
        apply_migrations(self.connection)
        sync_taxonomy(self.connection, load_taxonomy_document(ROOT / "config" / "taxonomy.yaml"))
        self.base_store = SQLiteStateStore(self.connection)
        self.base_store.save_canonical_track(
            CanonicalTrack("canonical.synthetic.phase5", "Glass Comet", ("Invented North",))
        )
        self.store = ResearchStore(self.connection)
        self.policy = load_research_policy_document(ROOT / "config" / "research.yaml")

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def coordinator(self, providers: list[ResearchProvider]) -> ResearchCoordinator:
        return ResearchCoordinator(
            store=self.store, providers=providers, policy=self.policy,
            phase4_bundle_digest="a" * 64, now=lambda: NOW, sleep=lambda _: None,
        )

    def test_provider_contract_contains_no_classification_or_playlist_fields(self) -> None:
        forbidden = {"playlist_id", "logical_playlist_id", "output_target_id", "classification", "proposal"}
        for contract in (ResearchRequest, ResearchResult, ProviderAssertion):
            self.assertTrue(forbidden.isdisjoint({item.name for item in fields(contract)}))
        self.assertNotIn("sufficient_for_classification", {item.name for item in fields(ResearchResult)})

    def test_probable_identifier_is_not_promoted_for_downstream_exact_lookup(self) -> None:
        assertion = ProviderAssertion(
            "identifier", EvidenceType.INDEPENDENT_IDENTIFIER,
            {"claim": "musicbrainz_recording_id", "namespace": "musicbrainz_recording",
             "value": "11111111-2222-3333-4444-555555555555"},
            EvidenceKind.SOURCED_FACT, ResearchSource.MUSICBRAINZ,
            "https://example.invalid/recording", SubjectScope.RECORDING,
            SubjectMatch.PROBABLE, quality_band=QualityBand.MEDIUM,
        )
        result = ResearchResult(
            "request.synthetic", "musicbrainz", "musicbrainz.v1", "query.synthetic",
            "q" * 64, iso(), ProviderOutcomeStatus.SUCCESS, (assertion,),
        )
        self.assertEqual((), ResearchCoordinator._independent_identifiers(result))

    def test_active_exact_identifier_evidence_is_propagated_to_later_provider(self) -> None:
        self.base_store.save_evidence(ResearchEvidence(
            "evidence.synthetic.musicbrainz-id", "canonical.synthetic.phase5",
            ResearchSource.MUSICBRAINZ, EvidenceKind.SOURCED_FACT,
            json.dumps({
                "claim": "musicbrainz_recording_id", "namespace": "musicbrainz_recording",
                "value": "11111111-2222-3333-4444-555555555555",
            }),
            "https://example.invalid/musicbrainz", 0.9,
            provider_id="musicbrainz", provider_version="musicbrainz.v1",
            request_id="request.synthetic.musicbrainz", query_id="query.synthetic.musicbrainz",
            subject_fingerprint="f" * 64, subject_scope="recording", subject_match="exact",
            evidence_type=EvidenceType.INDEPENDENT_IDENTIFIER.value,
            claim_key="musicbrainz_recording_id", quality_band="high",
            normalized_schema_version="research-evidence.v1",
            normalizer_version="evidence-normalizer.v1", retrieved_at=iso(),
            fresh_until=iso(NOW + timedelta(days=1)), content_digest="a" * 64,
        ))
        wikidata = FakeProvider("wikidata", [ProviderOutcomeStatus.NO_RESULT])
        self.coordinator([wikidata]).run(
            ("canonical.synthetic.phase5",), provider_id="wikidata"
        )
        self.assertEqual(
            ("musicbrainz_recording", "11111111-2222-3333-4444-555555555555"),
            (wikidata.requests[0].independent_identifiers[0].namespace,
             wikidata.requests[0].independent_identifiers[0].value),
        )

    def test_distinct_versions_have_distinct_fingerprints_without_merging(self) -> None:
        studio = subject_fingerprint(
            canonical_track_id="canonical.synthetic.studio", title="Same Name",
            artists=("Invented Act",), version_cues=("Studio",), identifiers=(),
        )
        live = subject_fingerprint(
            canonical_track_id="canonical.synthetic.live", title="Same Name",
            artists=("Invented Act",), version_cues=("Live",), identifiers=(),
        )
        self.assertNotEqual(studio, live)

    def test_fresh_cache_skips_provider_and_refresh_bypasses_it(self) -> None:
        provider = FakeProvider("musicbrainz", [ProviderOutcomeStatus.SUCCESS])
        first = self.coordinator([provider]).run(
            ("canonical.synthetic.phase5",), provider_id="musicbrainz"
        )
        second = self.coordinator([provider]).run(
            ("canonical.synthetic.phase5",), provider_id="musicbrainz"
        )
        refreshed = self.coordinator([provider]).run(
            ("canonical.synthetic.phase5",), provider_id="musicbrainz", refresh=True
        )
        self.assertEqual((1, 0, 1), (first.provider_calls, first.cache_hits, first.evidence_created))
        self.assertEqual((0, 1), (second.provider_calls, second.cache_hits))
        self.assertEqual(1, refreshed.provider_calls)
        self.assertEqual(2, provider.calls)
        self.assertEqual(1, len([item for item in self.store.evidence_for_track(
            "canonical.synthetic.phase5") if item.evidence_state == "active"]))

    def test_negative_cache_and_transient_retry_behavior(self) -> None:
        no_result = FakeProvider("musicbrainz", [ProviderOutcomeStatus.NO_RESULT])
        self.coordinator([no_result]).run(("canonical.synthetic.phase5",), provider_id="musicbrainz")
        cached = self.coordinator([no_result]).run(
            ("canonical.synthetic.phase5",), provider_id="musicbrainz"
        )
        self.assertEqual(1, cached.cache_hits)
        self.assertEqual(1, no_result.calls)

        retry = FakeProvider("wikidata", [
            ProviderOutcomeStatus.TRANSIENT_ERROR, ProviderOutcomeStatus.SUCCESS,
        ])
        summary = self.coordinator([retry]).run(
            ("canonical.synthetic.phase5",), provider_id="wikidata"
        )
        self.assertEqual(2, summary.provider_calls)
        self.assertEqual(2, retry.calls)
        attempts = [row for row in self.store.attempts_for_track("canonical.synthetic.phase5")
                    if row["provider_id"] == "wikidata"]
        self.assertEqual(["transient_error", "success"], [row["outcome"] for row in attempts])

    def test_adapter_and_request_schema_changes_bypass_old_cache(self) -> None:
        first = FakeProvider("musicbrainz", [ProviderOutcomeStatus.NO_RESULT])
        self.coordinator([first]).run(("canonical.synthetic.phase5",), provider_id="musicbrainz")
        changed_adapter = FakeProvider(
            "musicbrainz", [ProviderOutcomeStatus.NO_RESULT], adapter_version="musicbrainz.v2"
        )
        self.coordinator([changed_adapter]).run(
            ("canonical.synthetic.phase5",), provider_id="musicbrainz"
        )
        self.assertEqual(1, changed_adapter.calls)

        changed_schema = self.policy.model_copy(update={"request_schema_version": "research-request.v2"})
        schema_provider = FakeProvider("musicbrainz", [ProviderOutcomeStatus.NO_RESULT])
        ResearchCoordinator(
            store=self.store, providers=[schema_provider], policy=changed_schema,
            phase4_bundle_digest="a" * 64, now=lambda: NOW, sleep=lambda _: None,
        ).run(("canonical.synthetic.phase5",), provider_id="musicbrainz")
        self.assertEqual(1, schema_provider.calls)

    def test_failed_provider_does_not_erase_successful_provider_evidence(self) -> None:
        success = FakeProvider("musicbrainz", [ProviderOutcomeStatus.SUCCESS])
        failure = FakeProvider("wikidata", [ProviderOutcomeStatus.UNAVAILABLE])
        summary = self.coordinator([success, failure]).run(("canonical.synthetic.phase5",))
        self.assertEqual(2, summary.provider_calls)
        self.assertEqual(1, len(self.store.evidence_for_track("canonical.synthetic.phase5")))
        status = self.connection.execute(
            "SELECT status FROM research_jobs WHERE job_id = ?", (summary.job_ids[0],)
        ).fetchone()[0]
        self.assertEqual("partial", status)

    def test_contradictory_providers_remain_independent(self) -> None:
        self.coordinator([
            FakeProvider("musicbrainz", [ProviderOutcomeStatus.SUCCESS], value="rock"),
            FakeProvider("wikidata", [ProviderOutcomeStatus.SUCCESS], value="electronic"),
        ]).run(("canonical.synthetic.phase5",))
        report = EvidenceSufficiencyService(
            store=self.store,
            descriptors=tuple(FakeProvider(item, [ProviderOutcomeStatus.SUCCESS]).descriptor
                              for item in ("musicbrainz", "wikidata")),
            phase4_bundle_digest="b" * 64,
        ).assess("canonical.synthetic.phase5", now=iso())
        style = next(item for item in report.assessments
                     if item.evidence_type is EvidenceType.STYLE_TRADITION)
        self.assertEqual(SufficiencyState.CONTRADICTORY, style.state)
        self.assertEqual(2, len(style.evidence_ids))
        self.assertEqual("b" * 64, report.phase4_bundle_digest)

    def test_all_sufficiency_states_are_explicit(self) -> None:
        descriptors = (FakeProvider("musicbrainz", [ProviderOutcomeStatus.SUCCESS]).descriptor,)
        service = EvidenceSufficiencyService(
            store=self.store, descriptors=descriptors, phase4_bundle_digest="c" * 64
        )
        base = dict(
            canonical_track_id="canonical.synthetic.phase5", source=ResearchSource.MUSICBRAINZ,
            kind=EvidenceKind.SOURCED_FACT, source_reference="https://example.invalid/evidence",
            provider_id="musicbrainz", provider_version="musicbrainz.v1",
            request_id="request.synthetic", query_id="query.synthetic",
            subject_fingerprint="f" * 64, subject_scope="recording",
            normalized_schema_version="research-evidence.v1",
            normalizer_version="evidence-normalizer.v1", retrieved_at=iso(),
            fresh_until=iso(NOW + timedelta(days=1)), evidence_state="active",
        )
        cases = [
            ("sat", EvidenceType.RECORDING_IDENTITY, "exact", "high", {"claim": "identity", "value": "x"}),
            ("low", EvidenceType.VERSION_INFORMATION, "exact", "low", {"claim": "version", "value": "x"}),
            ("amb", EvidenceType.WORK_CONTEXT, "contextual", "high", {"claim": "work", "value": "x"}),
            ("na", EvidenceType.RELEASE, "exact", "high", {"claim": "release", "not_applicable": True}),
        ]
        import json
        for key, evidence_type, match, quality, value in cases:
            self.base_store.save_evidence(ResearchEvidence(
                evidence_id=f"evidence.synthetic.{key}", normalized_value=json.dumps(value),
                subject_match=match, evidence_type=evidence_type.value,
                claim_key=str(value["claim"]), quality_band=quality,
                content_digest=key * 32, **base,
            ))
        self.base_store.save_evidence(ResearchEvidence(
            evidence_id="evidence.synthetic.stale", normalized_value='{"claim":"tempo","value":"steady"}',
            subject_match="exact", evidence_type=EvidenceType.TEMPO_RHYTHM.value,
            claim_key="tempo", quality_band="high", content_digest="s" * 64,
            fresh_until=iso(NOW - timedelta(seconds=1)), **{k: v for k, v in base.items() if k != "fresh_until"},
        ))
        before_attempt = service.assess("canonical.synthetic.phase5", now=iso())
        before_states = {item.evidence_type: item.state for item in before_attempt.assessments}
        self.assertEqual(SufficiencyState.NOT_ASSESSED, before_states[EvidenceType.ATMOSPHERE])
        self.connection.execute(
            """INSERT INTO research_jobs VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)""",
            ("job.unavailable", "canonical.synthetic.phase5", "request.unavailable", "f" * 64,
             "research.v1", "c" * 64, iso(), iso()),
        )
        self.connection.execute(
            """INSERT INTO research_provider_attempts VALUES
               (?, ?, 'musicbrainz', 'musicbrainz.v1', ?, ?, 'unavailable', 0, 1, NULL, ?, ?, NULL, ?)""",
            ("attempt.unavailable", "job.unavailable", "query.unavailable", "q" * 64,
             iso(), iso(), "provider_unavailable"),
        )
        self.connection.commit()
        unavailable_report = service.assess("canonical.synthetic.phase5", now=iso())
        unavailable_states = {item.evidence_type: item.state for item in unavailable_report.assessments}
        self.assertEqual(SufficiencyState.PROVIDER_UNAVAILABLE,
                         unavailable_states[EvidenceType.ARRANGEMENT])
        self.connection.execute(
            """INSERT INTO research_provider_attempts VALUES
               (?, ?, 'musicbrainz', 'musicbrainz.v1', ?, ?, 'success', 0, 2, NULL, ?, ?, NULL, NULL)""",
            ("attempt.success-empty", "job.unavailable", "query.success-empty", "r" * 64,
             iso(), iso()),
        )
        self.connection.commit()
        report = service.assess("canonical.synthetic.phase5", now=iso())
        states = {item.evidence_type: item.state for item in report.assessments}
        self.assertEqual(SufficiencyState.SATISFIED, states[EvidenceType.RECORDING_IDENTITY])
        self.assertEqual(SufficiencyState.LOW_QUALITY, states[EvidenceType.VERSION_INFORMATION])
        self.assertEqual(SufficiencyState.SUBJECT_AMBIGUOUS, states[EvidenceType.WORK_CONTEXT])
        self.assertEqual(SufficiencyState.NOT_APPLICABLE, states[EvidenceType.RELEASE])
        self.assertEqual(SufficiencyState.STALE, states[EvidenceType.TEMPO_RHYTHM])
        self.assertEqual(SufficiencyState.MISSING, states[EvidenceType.ARRANGEMENT])

    def test_phase4_digest_recomputes_sufficiency_without_expiring_evidence(self) -> None:
        provider = FakeProvider("musicbrainz", [ProviderOutcomeStatus.SUCCESS])
        self.coordinator([provider]).run(("canonical.synthetic.phase5",), provider_id="musicbrainz")
        descriptor = (provider.descriptor,)
        first = EvidenceSufficiencyService(
            store=self.store, descriptors=descriptor, phase4_bundle_digest="1" * 64
        ).assess("canonical.synthetic.phase5", now=iso())
        second = EvidenceSufficiencyService(
            store=self.store, descriptors=descriptor, phase4_bundle_digest="2" * 64
        ).assess("canonical.synthetic.phase5", now=iso())
        self.assertNotEqual(first.phase4_bundle_digest, second.phase4_bundle_digest)
        self.assertEqual(first.assessments, second.assessments)
        self.assertEqual("active", self.store.evidence_for_track(
            "canonical.synthetic.phase5"
        )[0].evidence_state)

    def test_migration_four_to_five_preserves_legacy_evidence(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.db"
        connection = connect_database(legacy_path)
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT UNIQUE, "
            "checksum TEXT, applied_at TEXT)"
        )
        for migration in load_migrations()[:4]:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utc_now()),
            )
        connection.execute(
            "INSERT INTO canonical_tracks VALUES (?, ?, ?)",
            ("canonical.synthetic.legacy", "Legacy Signal", utc_now()),
        )
        connection.execute(
            "INSERT INTO research_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("evidence.synthetic.legacy", "canonical.synthetic.legacy", "publication",
             "sourced_fact", "legacy value", None, None, utc_now()),
        )
        connection.commit()
        self.assertEqual(4, current_schema_version(connection))
        self.assertEqual(5, apply_migrations(connection))
        row = connection.execute(
            "SELECT normalized_value, provider_id, evidence_state FROM research_evidence"
        ).fetchone()
        self.assertEqual(("legacy value", None, "active"), tuple(row))
        assert_integrity(connection)
        self.assertEqual(0, connection.execute("PRAGMA foreign_key_check").fetchall().__len__())
        connection.close()


if __name__ == "__main__":
    unittest.main()
