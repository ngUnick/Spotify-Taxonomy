from __future__ import annotations

from pathlib import Path
import unittest

import httpx
from pydantic import ValidationError

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.adapters.research import (
    MusicBrainzResearchProvider, WikidataResearchProvider,
)
from spotify_playlist_taxonomy.adapters.research.musicbrainz import MusicBrainzRequestGate
from spotify_playlist_taxonomy.config import (
    ResearchProviderPolicy, load_research_policy_document,
)
from spotify_playlist_taxonomy.domain.research import EvidenceType, SubjectMatch, SubjectScope
from spotify_playlist_taxonomy.ports.research import (
    IndependentIdentifier, ProviderOutcomeStatus, ResearchRequest,
)

ROOT = Path(__file__).resolve().parents[1]
MBID = "11111111-2222-3333-4444-555555555555"


def request(*identifiers: IndependentIdentifier) -> ResearchRequest:
    return ResearchRequest(
        "request.synthetic.provider", "research-request.v1",
        "canonical.synthetic.provider", "f" * 64, "Prism Harbor",
        ("Invented Meridian",), ("Studio",), tuple(identifiers),
        tuple(EvidenceType), "2026-01-02T03:04:05Z",
    )


def entity_claim(entity_id: str) -> dict[str, object]:
    return {"mainsnak": {"datavalue": {"value": {"id": entity_id}}}}


def music_entity(
    title: str, artist_id: str, *, aliases: tuple[str, ...] = (), entity_type: str = "Q7366",
) -> dict[str, object]:
    return {
        "labels": {"en": {"value": title}},
        "aliases": {"en": [{"value": value} for value in aliases]},
        "claims": {"P31": [entity_claim(entity_type)], "P175": [entity_claim(artist_id)]},
    }


def artist_entity(name: str, *, aliases: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "labels": {"en": {"value": name}},
        "aliases": {"el": [{"value": value} for value in aliases]},
        "claims": {},
    }


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        policy = load_research_policy_document(ROOT / "config" / "research.yaml")
        self.musicbrainz_policy = next(item for item in policy.providers if item.provider_id == "musicbrainz")
        self.wikidata_policy = next(item for item in policy.providers if item.provider_id == "wikidata")

    def test_musicbrainz_exact_lookup_emits_only_supported_structured_evidence(self) -> None:
        payload = {
            "id": MBID,
            "title": "Prism Harbor",
            "disambiguation": "studio version",
            "artist-credit": [{"name": "Invented Meridian"}],
            "releases": [{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}],
            "relations": [{"work": {"id": "99999999-8888-7777-6666-555555555555"}}],
            "tags": [{"name": "must-not-be-used"}],
            "rating": {"value": 5},
        }
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        with httpx.Client(transport=transport) as client:
            result = MusicBrainzResearchProvider(
                client=client, policy=self.musicbrainz_policy
            ).research(request(IndependentIdentifier("musicbrainz_recording", MBID)))
        self.assertEqual(ProviderOutcomeStatus.SUCCESS, result.status)
        types = {item.evidence_type for item in result.assertions}
        self.assertIn(EvidenceType.INDEPENDENT_IDENTIFIER, types)
        self.assertIn(EvidenceType.WORK_CONTEXT, types)
        self.assertNotIn(EvidenceType.STYLE_TRADITION, types)
        recording_items = [item for item in result.assertions if item.subject_scope is SubjectScope.RECORDING]
        work_items = [item for item in result.assertions if item.subject_scope is SubjectScope.WORK]
        self.assertTrue(all(item.subject_match is SubjectMatch.EXACT for item in recording_items))
        self.assertTrue(all(item.subject_match is SubjectMatch.CONTEXTUAL for item in work_items))
        self.assertNotIn("must-not-be-used", repr(result.assertions))

    def test_musicbrainz_title_artist_discovery_is_probable_never_exact(self) -> None:
        payload = {
            "recordings": [{
                "id": MBID, "title": "Prism Harbor",
                "artist-credit": [{"name": "Invented Meridian"}],
            }]
        }
        with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload)
        )) as client:
            result = MusicBrainzResearchProvider(
                client=client, policy=self.musicbrainz_policy
            ).research(request())
        self.assertEqual(ProviderOutcomeStatus.SUCCESS, result.status)
        self.assertTrue(all(item.subject_match is SubjectMatch.PROBABLE for item in result.assertions))
        identifiers = [item for item in result.assertions
                       if item.evidence_type is EvidenceType.INDEPENDENT_IDENTIFIER]
        self.assertTrue(identifiers)
        self.assertTrue(all(item.subject_match is SubjectMatch.PROBABLE for item in identifiers))

    def test_wikidata_exact_identifier_preserves_statement_and_context_scope(self) -> None:
        payload = {"results": {"bindings": [
            {
                "item": {"value": "http://www.wikidata.org/entity/Q123"},
                "statement": {"value": "http://www.wikidata.org/entity/statement/Q123-A"},
                "claim": {"value": "genre"},
                "value": {"value": "http://www.wikidata.org/entity/Q456"},
                "valueLabel": {"value": "Synthetic Rock"},
            },
            {
                "item": {"value": "http://www.wikidata.org/entity/Q123"},
                "statement": {"value": "http://www.wikidata.org/entity/statement/Q123-B"},
                "claim": {"value": "composer"},
                "value": {"value": "http://www.wikidata.org/entity/Q789"},
                "valueLabel": {"value": "Invented Composer"},
            },
            {
                "item": {"value": "http://www.wikidata.org/entity/Q123"},
                "statement": {"value": "http://www.wikidata.org/entity/statement/Q123-C"},
                "claim": {"value": "performer"},
                "value": {"value": "http://www.wikidata.org/entity/Q321"},
                "valueLabel": {"value": "Invented Project"},
            },
        ]}}
        with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload)
        )) as client:
            result = WikidataResearchProvider(
                client=client, policy=self.wikidata_policy
            ).research(request(IndependentIdentifier("musicbrainz_recording", MBID)))
        self.assertEqual(ProviderOutcomeStatus.SUCCESS, result.status)
        genre = next(item for item in result.assertions if item.evidence_type is EvidenceType.STYLE_TRADITION)
        composer = next(item for item in result.assertions if item.asserted_value["claim"] == "wikidata_composer")
        project = next(item for item in result.assertions
                       if item.evidence_type is EvidenceType.PROJECT_SCENE_CONTEXT)
        identifier = next(item for item in result.assertions
                          if item.evidence_type is EvidenceType.INDEPENDENT_IDENTIFIER)
        self.assertEqual((SubjectScope.RECORDING, SubjectMatch.EXACT),
                         (genre.subject_scope, genre.subject_match))
        self.assertEqual((SubjectScope.WORK, SubjectMatch.CONTEXTUAL),
                         (composer.subject_scope, composer.subject_match))
        self.assertEqual((SubjectScope.PROJECT, SubjectMatch.CONTEXTUAL),
                         (project.subject_scope, project.subject_match))
        self.assertEqual(SubjectMatch.EXACT, identifier.subject_match)
        self.assertIn("statement/Q123-A", genre.source_reference)

    def test_wikidata_weak_discovery_fails_closed_on_ambiguity(self) -> None:
        calls = []
        def handler(request_value: httpx.Request) -> httpx.Response:
            calls.append(request_value)
            if "wbsearchentities" in str(request_value.url):
                return httpx.Response(200, json={"search": [{"id": "Q123"}, {"id": "Q456"}]})
            ids = str(request_value.url.params["ids"]).split("|")
            entities = {
                "Q123": music_entity("Prism Harbor", "Q321"),
                "Q456": music_entity("Prism Harbor", "Q321", entity_type="Q134556"),
                "Q321": artist_entity("Invented Meridian"),
            }
            return httpx.Response(200, json={"entities": {key: entities[key] for key in ids}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = WikidataResearchProvider(
                client=client, policy=self.wikidata_policy
            ).research(request())
        self.assertEqual(ProviderOutcomeStatus.AMBIGUOUS_SUBJECT, result.status)
        self.assertEqual(3, len(calls))
        self.assertFalse(result.assertions)

    def test_wikidata_title_artist_discovery_is_usable_without_musicbrainz(self) -> None:
        def handler(value: httpx.Request) -> httpx.Response:
            if "wbsearchentities" in str(value.url):
                return httpx.Response(200, json={"search": [{"id": "Q123", "label": "Prism Harbor"}]})
            if "wbgetentities" in str(value.url):
                ids = str(value.url.params["ids"]).split("|")
                entities = {
                    "Q123": music_entity("Prism Harbor", "Q321"),
                    "Q321": artist_entity("Invented Meridian"),
                }
                return httpx.Response(200, json={"entities": {key: entities[key] for key in ids}})
            return httpx.Response(200, json={"results": {"bindings": [{
                "item": {"value": "http://www.wikidata.org/entity/Q123"},
                "statement": {"value": "http://www.wikidata.org/entity/statement/Q123-A"},
                "claim": {"value": "genre"},
                "value": {"value": "http://www.wikidata.org/entity/Q456"},
                "valueLabel": {"value": "Synthetic Style"},
            }]}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = WikidataResearchProvider(client=client, policy=self.wikidata_policy).research(request())
        self.assertEqual(ProviderOutcomeStatus.SUCCESS, result.status)
        self.assertTrue(all(item.subject_match is not SubjectMatch.EXACT for item in result.assertions))

    def test_title_discovery_handles_punctuation_case_aliases_and_song_or_single_types(self) -> None:
        cases = (
            ("Salt & Pepper", "Salt & Pepper", (), "Q7366"),
            ("Don't Stop", "DON'T STOP", (), "Q134556"),
            ("Night Drive", "Different Primary", ("NIGHT DRIVE",), "Q207628"),
        )
        for title, label, aliases, entity_type in cases:
            with self.subTest(title=title):
                def handler(value: httpx.Request) -> httpx.Response:
                    if "wbsearchentities" in str(value.url):
                        return httpx.Response(200, json={"search": [{"id": "Q123", "label": "index value"}]})
                    if "wbgetentities" in str(value.url):
                        ids = str(value.url.params["ids"]).split("|")
                        entities = {
                            "Q123": music_entity(label, "Q321", aliases=aliases, entity_type=entity_type),
                            "Q321": artist_entity("Invented Meridian"),
                        }
                        return httpx.Response(200, json={"entities": {key: entities[key] for key in ids}})
                    return httpx.Response(200, json={"results": {"bindings": [{
                        "item": {"value": "http://www.wikidata.org/entity/Q123"},
                        "statement": {"value": "http://www.wikidata.org/entity/statement/Q123-A"},
                        "claim": {"value": "genre"},
                        "value": {"value": "http://www.wikidata.org/entity/Q456"},
                        "valueLabel": {"value": "Synthetic Style"},
                    }]}})
                custom = ResearchRequest(
                    "request.synthetic.provider", "research-request.v1", "canonical.synthetic.provider",
                    "f" * 64, title, ("Invented Meridian",), (), (), tuple(EvidenceType),
                    "2026-01-02T03:04:05Z",
                )
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = WikidataResearchProvider(client=client, policy=self.wikidata_policy).research(custom)
                self.assertEqual(ProviderOutcomeStatus.SUCCESS, result.status)

    def test_title_search_is_not_combined_with_artist_and_wrong_performer_is_rejected(self) -> None:
        search_values: list[str] = []
        def handler(value: httpx.Request) -> httpx.Response:
            if "wbsearchentities" in str(value.url):
                search_values.append(str(value.url.params["search"]))
                return httpx.Response(200, json={"search": [{"id": "Q123"}]})
            ids = str(value.url.params["ids"]).split("|")
            entities = {
                "Q123": music_entity("Prism Harbor", "Q321"),
                "Q321": artist_entity("Wrong Artist"),
            }
            return httpx.Response(200, json={"entities": {key: entities[key] for key in ids}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = WikidataResearchProvider(client=client, policy=self.wikidata_policy).research(request())
        self.assertEqual(["Prism Harbor"], search_values)
        self.assertEqual(ProviderOutcomeStatus.NO_RESULT, result.status)
        self.assertEqual("performer_mismatch", result.error_category)

    def test_contributor_qid_alias_and_multiple_credits_are_resolved_before_matching(self) -> None:
        def handler(value: httpx.Request) -> httpx.Response:
            if "wbsearchentities" in str(value.url):
                return httpx.Response(200, json={"search": [{"id": "Q123"}]})
            if "wbgetentities" in str(value.url):
                ids = str(value.url.params["ids"]).split("|")
                candidate = music_entity("Prism Harbor", "Q321")
                candidate["claims"]["P175"].append(entity_claim("Q322"))  # type: ignore[index]
                entities = {
                    "Q123": candidate,
                    "Q321": artist_entity("Different Legal Name", aliases=("Invented Meridian",)),
                    "Q322": artist_entity("Other Performer"),
                }
                return httpx.Response(200, json={"entities": {key: entities[key] for key in ids}})
            return httpx.Response(200, json={"results": {"bindings": [{
                "item": {"value": "http://www.wikidata.org/entity/Q123"},
                "statement": {"value": "http://www.wikidata.org/entity/statement/Q123-A"},
                "claim": {"value": "genre"},
                "value": {"value": "http://www.wikidata.org/entity/Q456"},
                "valueLabel": {"value": "Synthetic Style"},
            }]}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = WikidataResearchProvider(client=client, policy=self.wikidata_policy).research(request())
        self.assertEqual(ProviderOutcomeStatus.SUCCESS, result.status)
        self.assertTrue(all(item.subject_match is SubjectMatch.PROBABLE for item in result.assertions))

    def test_unresolved_contributor_and_conflicting_version_cue_are_not_accepted(self) -> None:
        for mode, expected in (("unresolved", "performer_unresolved"), ("version", "version_conflict")):
            with self.subTest(mode=mode):
                def handler(value: httpx.Request) -> httpx.Response:
                    if "wbsearchentities" in str(value.url):
                        return httpx.Response(200, json={"search": [{"id": "Q123"}]})
                    ids = str(value.url.params["ids"]).split("|")
                    candidate = music_entity(
                        "Prism Harbor Remix" if mode == "version" else "Prism Harbor", "Q321"
                    )
                    if mode == "version":
                        candidate["aliases"] = {"en": [{"value": "Prism Harbor"}]}
                    entities = {"Q123": candidate}
                    if mode == "version":
                        entities["Q321"] = artist_entity("Invented Meridian")
                    return httpx.Response(200, json={"entities": {key: entities[key] for key in ids if key in entities}})
                custom = ResearchRequest(
                    "request.synthetic.provider", "research-request.v1", "canonical.synthetic.provider",
                    "f" * 64, "Prism Harbor", ("Invented Meridian",),
                    ("Live",) if mode == "version" else (), (), tuple(EvidenceType),
                    "2026-01-02T03:04:05Z",
                )
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = WikidataResearchProvider(client=client, policy=self.wikidata_policy).research(custom)
                self.assertEqual(ProviderOutcomeStatus.NO_RESULT, result.status)
                self.assertEqual(expected, result.error_category)

    def test_wikidata_no_candidate_and_no_supported_claims_have_distinct_reasons(self) -> None:
        no_candidate = {"results": {"bindings": []}}
        candidate_without_claims = {"results": {"bindings": [{
            "item": {"value": "http://www.wikidata.org/entity/Q123"},
        }]}}
        for payload, reason in (
            (no_candidate, "no_candidate_found"),
            (candidate_without_claims, "candidate_found_no_supported_claims"),
        ):
            with self.subTest(reason=reason), httpx.Client(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=payload)
            )) as client:
                result = WikidataResearchProvider(
                    client=client, policy=self.wikidata_policy
                ).research(request(IndependentIdentifier("musicbrainz_recording", MBID)))
            self.assertEqual(ProviderOutcomeStatus.NO_RESULT, result.status)
            self.assertEqual(reason, result.error_category)
            self.assertFalse(result.assertions)
            self.assertNotIn("Synthetic", repr(result))

    def test_transport_errors_are_sanitized(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("secret-token-should-not-escape")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MusicBrainzResearchProvider(
                client=client, policy=self.musicbrainz_policy
            ).research(request())
        self.assertEqual(ProviderOutcomeStatus.TRANSIENT_ERROR, result.status)
        self.assertEqual("transport_error", result.error_category)
        self.assertNotIn("secret-token", repr(result))

    def test_musicbrainz_isrc_lookup_normalizes_deduplicates_and_orders_recordings(self) -> None:
        other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        requests: list[httpx.Request] = []
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"recordings": [{"id": other.upper()}, {"id": MBID}, {"id": other}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy).lookup_recordings_by_isrc("us-rc1-76-07839")
        self.assertEqual("USRC17607839", result.normalized_isrc)
        self.assertEqual((MBID, other), result.recording_ids)
        self.assertIsNone(result.failure_category)
        self.assertEqual("/ws/2/isrc/USRC17607839", requests[0].url.path)
        self.assertEqual(self.musicbrainz_policy.user_agent, requests[0].headers["User-Agent"])
        self.assertNotIn("recordings", repr(result))

    def test_musicbrainz_isrc_request_gate_paces_shared_provider_instances_without_real_sleep(self) -> None:
        class Clock:
            def __init__(self) -> None: self.value = 0.0; self.sleeps: list[float] = []
            def monotonic(self) -> float: return self.value
            def sleep(self, seconds: float) -> None: self.sleeps.append(seconds); self.value += seconds
        clock = Clock(); gate = MusicBrainzRequestGate(monotonic=clock.monotonic, sleep=clock.sleep)
        request_times: list[float] = []
        def handler(_: httpx.Request) -> httpx.Response:
            request_times.append(clock.value); return httpx.Response(200, json={"recordings": []})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            first = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy, request_gate=gate)
            second = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy, request_gate=gate)
            first.lookup_recordings_by_isrc("USRC17607839")
            second.lookup_recordings_by_isrc("USRC17607839")
            clock.value += 1.0
            first.lookup_recordings_by_isrc("USRC17607839")
        self.assertEqual([0.0, 1.0, 2.0], request_times)
        self.assertEqual([1.0], clock.sleeps)

    def test_musicbrainz_isrc_retries_only_transient_results_with_pacing_and_backoff(self) -> None:
        class Clock:
            def __init__(self) -> None: self.value = 0.0; self.sleeps: list[float] = []
            def monotonic(self) -> float: return self.value
            def sleep(self, seconds: float) -> None: self.sleeps.append(seconds); self.value += seconds
        def lookup(responses: list[object]) -> tuple[object, list[httpx.Request], Clock]:
            clock = Clock(); gate = MusicBrainzRequestGate(monotonic=clock.monotonic, sleep=clock.sleep); requests: list[httpx.Request] = []
            def handler(value: httpx.Request) -> httpx.Response:
                requests.append(value); outcome = responses.pop(0)
                if isinstance(outcome, Exception): raise outcome
                return outcome  # type: ignore[return-value]
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                result = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy, request_gate=gate, lookup_max_attempts=3, sleep=clock.sleep).lookup_recordings_by_isrc("USRC17607839")
            return result, requests, clock
        for response in (httpx.Response(503), httpx.Response(429)):
            with self.subTest(status=response.status_code):
                result, requests, clock = lookup([response, httpx.Response(200, json={"recordings": [{"id": MBID}]})])
                self.assertEqual((MBID,), result.recording_ids); self.assertEqual(2, len(requests)); self.assertEqual([1.0], clock.sleeps)
        result, requests, _ = lookup([httpx.Response(503), httpx.Response(503), httpx.Response(503)])
        self.assertEqual(((), "provider_temporarily_unavailable", 3), (result.recording_ids, result.failure_category, len(requests)))
        result, requests, _ = lookup([httpx.ReadTimeout("synthetic"), httpx.Response(200, json={"recordings": []})])
        self.assertEqual(((), None, 2), (result.recording_ids, result.failure_category, len(requests)))
        result, requests, _ = lookup([httpx.ConnectError("synthetic"), httpx.Response(200, json={"recordings": []})])
        self.assertEqual(((), None, 2), (result.recording_ids, result.failure_category, len(requests)))
        result, requests, _ = lookup([httpx.Response(200, json={"recordings": []}), httpx.Response(200, json={"recordings": [{"id": MBID}]})])
        self.assertEqual(((), None, 1), (result.recording_ids, result.failure_category, len(requests)))
        result, requests, _ = lookup([httpx.Response(200, json={"recordings": [{"id": "invalid"}]})])
        self.assertEqual(("invalid_response", 1), (result.failure_category, len(requests)))
        invalid_calls = 0
        def invalid_handler(_: httpx.Request) -> httpx.Response:
            nonlocal invalid_calls; invalid_calls += 1; return httpx.Response(200)
        with httpx.Client(transport=httpx.MockTransport(invalid_handler)) as client:
            provider = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy, request_gate=MusicBrainzRequestGate(monotonic=lambda: 0.0, sleep=lambda _: None))
            with self.assertRaises(ValueError): provider.lookup_recordings_by_isrc("invalid")
        self.assertEqual(0, invalid_calls)

    def test_musicbrainz_isrc_retry_after_and_phase5_research_do_not_multiply_retries(self) -> None:
        class Clock:
            def __init__(self) -> None: self.value = 0.0; self.sleeps: list[float] = []
            def monotonic(self) -> float: return self.value
            def sleep(self, seconds: float) -> None: self.sleeps.append(seconds); self.value += seconds
        for retry_after, expected in (("0.25", [0.25, 0.75]), ("2", [2.0])):
            with self.subTest(retry_after=retry_after):
                clock = Clock(); gate = MusicBrainzRequestGate(monotonic=clock.monotonic, sleep=clock.sleep); calls = 0
                def handler(_: httpx.Request) -> httpx.Response:
                    nonlocal calls; calls += 1
                    return httpx.Response(503, headers={"Retry-After": retry_after}) if calls == 1 else httpx.Response(200, json={"recordings": []})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy, request_gate=gate, lookup_max_attempts=3, sleep=clock.sleep).lookup_recordings_by_isrc("USRC17607839")
                self.assertEqual(expected, clock.sleeps)
        calls = 0
        def phase5_handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls; calls += 1; return httpx.Response(503)
        clock = Clock()
        with httpx.Client(transport=httpx.MockTransport(phase5_handler)) as client:
            result = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy, request_gate=MusicBrainzRequestGate(monotonic=clock.monotonic, sleep=clock.sleep), lookup_max_attempts=3, sleep=clock.sleep).research(request())
        self.assertEqual((ProviderOutcomeStatus.TRANSIENT_ERROR, 1), (result.status, calls))

    def test_musicbrainz_isrc_lookup_fails_closed_for_malformed_or_provider_failure(self) -> None:
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": [{"id": "not-a-mbid"}]}))) as client:
            malformed = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy).lookup_recordings_by_isrc("USRC17607839")
        self.assertEqual((), malformed.recording_ids)
        self.assertEqual("invalid_response", malformed.failure_category)
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            failed = MusicBrainzResearchProvider(client=client, policy=self.musicbrainz_policy).lookup_recordings_by_isrc("USRC17607839")
        self.assertEqual("provider_temporarily_unavailable", failed.failure_category)

    def test_policy_rejects_unapproved_endpoint_and_unknown_provider(self) -> None:
        payload = self.musicbrainz_policy.model_dump(mode="json")
        payload["api_url"] = "https://example.invalid/ws"
        with self.assertRaises(ValidationError):
            ResearchProviderPolicy.model_validate(payload)
        payload["provider_id"] = "lastfm"
        with self.assertRaises(ValidationError):
            ResearchProviderPolicy.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
