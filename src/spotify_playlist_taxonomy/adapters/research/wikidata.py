"""Structured Wikidata statement adapter with explicit subject scope."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from spotify_playlist_taxonomy.config import ResearchProviderPolicy
from spotify_playlist_taxonomy.domain.models import EvidenceKind, ResearchSource
from spotify_playlist_taxonomy.domain.research import EvidenceType, QualityBand, SubjectMatch, SubjectScope
from spotify_playlist_taxonomy.ports.research import (
    ProviderAssertion, ProviderDescriptor, ProviderOutcomeStatus,
    ResearchRequest, ResearchResult,
)


CLAIMS = {
    "genre": (EvidenceType.STYLE_TRADITION, "wikidata_genre", SubjectScope.RECORDING),
    "language": (EvidenceType.LYRICAL_LANGUAGE, "wikidata_work_language", SubjectScope.WORK),
    "country": (EvidenceType.CULTURAL_REGIONAL_CONTEXT, "wikidata_country_of_origin", SubjectScope.WORK),
    "performer": (EvidenceType.CREDITS, "wikidata_performer", SubjectScope.RECORDING),
    "composer": (EvidenceType.CREDITS, "wikidata_composer", SubjectScope.WORK),
    "instance_of": (EvidenceType.WORK_CONTEXT, "wikidata_instance_of", SubjectScope.WORK),
    "part_of": (EvidenceType.WORK_CONTEXT, "wikidata_part_of", SubjectScope.WORK),
}

_MUSIC_ENTITY_TYPES = frozenset({"Q7366", "Q134556", "Q207628", "Q2188189"})
_CANDIDATE_LIMIT = 8


@dataclass(frozen=True, slots=True)
class _DiscoveredCandidate:
    entity_id: str
    subject_match: SubjectMatch


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalized_text(value: str) -> str:
    """Compare labels/aliases without treating fuzzy similarity as identity evidence."""

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return " ".join("".join(
        character if character.isalnum() else " "
        for character in decomposed
        if not unicodedata.combining(character)
    ).split())


class WikidataResearchProvider:
    def __init__(self, *, client: httpx.Client, policy: ResearchProviderPolicy):
        if policy.provider_id != "wikidata" or policy.query_url is None:
            raise ValueError("Wikidata adapter requires API and query URLs")
        self.client = client
        self.policy = policy
        self._descriptor = ProviderDescriptor(
            "wikidata", policy.adapter_version, "research-provider.v1",
            (EvidenceType.STYLE_TRADITION, EvidenceType.WORK_CONTEXT,
             EvidenceType.LYRICAL_LANGUAGE, EvidenceType.CULTURAL_REGIONAL_CONTEXT,
             EvidenceType.PROJECT_SCENE_CONTEXT, EvidenceType.CREDITS,
             EvidenceType.INDEPENDENT_IDENTIFIER),
            False, policy.minimum_interval_seconds,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def research(self, request: ResearchRequest) -> ResearchResult:
        wikidata_id = next((item.value for item in request.independent_identifiers
                            if item.namespace == "wikidata" and re.fullmatch(r"Q[1-9][0-9]*", item.value)), None)
        mbid = next((item.value for item in request.independent_identifiers
                     if item.namespace == "musicbrainz_recording"), None)
        if wikidata_id:
            item_selector = f"VALUES ?item {{ wd:{wikidata_id} }}"
            subject_match = SubjectMatch.EXACT
            discovery_key = {"wikidata": wikidata_id}
        elif mbid:
            item_selector = f'?item wdt:P4404 "{mbid}" .'
            subject_match = SubjectMatch.EXACT
            discovery_key = {"musicbrainz_recording": mbid}
        else:
            discovered = self._discover(request)
            if isinstance(discovered, ResearchResult):
                return discovered
            item_selector = f"VALUES ?item {{ wd:{discovered.entity_id} }}"
            subject_match = discovered.subject_match
            discovery_key = {"qid": discovered.entity_id}
        query = self._statement_query(item_selector)
        query_fingerprint = _fingerprint({"query": query, "discovery": discovery_key})
        query_id = f"wikidata-query.{query_fingerprint[:24]}"
        response = self._get(self.policy.query_url, {"query": query, "format": "json"},
                             request, query_id, query_fingerprint)
        if isinstance(response, ResearchResult):
            return response
        try:
            bindings = response.json()["results"]["bindings"]
        except (ValueError, TypeError, KeyError):
            return self._failure(request, query_id, query_fingerprint, "invalid_response")
        if not isinstance(bindings, list):
            return self._failure(request, query_id, query_fingerprint, "invalid_response")
        assertions: list[ProviderAssertion] = []
        identified_items: set[str] = set()
        candidate_items: set[str] = set()
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                continue
            claim = self._binding_value(binding, "claim")
            value = self._binding_value(binding, "valueLabel") or self._binding_value(binding, "value")
            item_uri = self._binding_value(binding, "item")
            statement_uri = self._binding_value(binding, "statement")
            if not item_uri:
                continue
            candidate_items.add(item_uri.rsplit("/", 1)[-1])
            if claim not in CLAIMS or not value:
                continue
            evidence_type, claim_key, scope = CLAIMS[claim]
            match = subject_match if scope is SubjectScope.RECORDING else SubjectMatch.CONTEXTUAL
            item_id = item_uri.rsplit("/", 1)[-1]
            reference = statement_uri or f"https://www.wikidata.org/wiki/{item_id}"
            if item_id not in identified_items:
                assertions.append(ProviderAssertion(
                    f"entity:{item_id}", EvidenceType.INDEPENDENT_IDENTIFIER,
                    {"claim": "wikidata_entity_id", "namespace": "wikidata", "value": item_id},
                    EvidenceKind.SOURCED_FACT, ResearchSource.WIKIDATA,
                    f"https://www.wikidata.org/wiki/{item_id}", SubjectScope.RECORDING,
                    subject_match,
                    quality_band=QualityBand.HIGH if subject_match is SubjectMatch.EXACT else QualityBand.MEDIUM,
                ))
                identified_items.add(item_id)
            assertions.append(ProviderAssertion(
                f"{claim}:{index}", evidence_type,
                {"claim": claim_key, "value": value, "entity_id": item_id},
                EvidenceKind.SOURCED_FACT, ResearchSource.WIKIDATA, reference, scope, match,
                quality_band=QualityBand.HIGH if subject_match is SubjectMatch.EXACT else QualityBand.MEDIUM,
            ))
            if claim == "performer":
                assertions.append(ProviderAssertion(
                    f"performing-project:{index}", EvidenceType.PROJECT_SCENE_CONTEXT,
                    {"claim": "wikidata_performing_project", "value": value,
                     "entity_id": item_id},
                    EvidenceKind.SOURCED_FACT, ResearchSource.WIKIDATA, reference,
                    SubjectScope.PROJECT, SubjectMatch.CONTEXTUAL,
                    quality_band=QualityBand.HIGH if subject_match is SubjectMatch.EXACT else QualityBand.MEDIUM,
                ))
        if not candidate_items:
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, query_fingerprint, _now(), ProviderOutcomeStatus.NO_RESULT,
                                  error_category="no_candidate_found")
        if not assertions:
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, query_fingerprint, _now(), ProviderOutcomeStatus.NO_RESULT,
                                  error_category="candidate_found_no_supported_claims")
        return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                              query_id, query_fingerprint, _now(), ProviderOutcomeStatus.SUCCESS,
                              tuple(assertions))

    def _discover(self, request: ResearchRequest) -> _DiscoveredCandidate | ResearchResult:
        params = {"action": "wbsearchentities", "search": request.title,
                  "language": "en", "type": "item", "format": "json", "limit": str(_CANDIDATE_LIMIT)}
        fingerprint = _fingerprint({"url": self.policy.api_url, "params": params})
        query_id = f"wikidata-discovery.{fingerprint[:24]}"
        response = self._get(self.policy.api_url, params, request, query_id, fingerprint)
        if isinstance(response, ResearchResult):
            return response
        try:
            search = response.json()["search"]
        except (ValueError, TypeError, KeyError):
            return self._failure(request, query_id, fingerprint, "invalid_response")
        candidate_ids = tuple(dict.fromkeys(
            str(item.get("id")) for item in search
            if isinstance(item, dict) and re.fullmatch(r"Q[1-9][0-9]*", str(item.get("id", "")))
        )) if isinstance(search, list) else ()
        if not candidate_ids:
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, fingerprint, _now(), ProviderOutcomeStatus.NO_RESULT,
                                  error_category="no_candidate_found")
        entities = self._entities(request, candidate_ids, query_id, fingerprint)
        if isinstance(entities, ResearchResult):
            return entities
        performer_ids = tuple(sorted({
            performer_id
            for entity in entities.values()
            for property_id in ("P175", "P86")
            for performer_id in self._claim_entity_ids(entity, property_id)
        }))
        performers = self._entities(request, performer_ids, query_id, fingerprint) if performer_ids else {}
        if isinstance(performers, ResearchResult):
            return performers
        title = _normalized_text(request.title)
        primary_artist = _normalized_text(request.artists[0])
        survivors: list[str] = []
        rejection_reasons: set[str] = set()
        for entity_id in candidate_ids:
            entity = entities.get(entity_id)
            if entity is None or not self._matches_title(entity, title):
                rejection_reasons.add("title_mismatch")
                continue
            contributor_names = {
                _normalized_text(label)
                for property_id in ("P175", "P86")
                for performer_id in self._claim_entity_ids(entity, property_id)
                for label in self._entity_names(performers.get(performer_id, {}))
            }
            is_music_entity = bool(set(self._claim_entity_ids(entity, "P31")) & _MUSIC_ENTITY_TYPES)
            if not is_music_entity:
                rejection_reasons.add("candidate_rejected_subject_match")
                continue
            if self._version_conflicts(request.version_cues, entity):
                rejection_reasons.add("version_conflict")
                continue
            contributor_ids = tuple(
                contributor_id for property_id in ("P175", "P86")
                for contributor_id in self._claim_entity_ids(entity, property_id)
            )
            if not contributor_ids or not contributor_names:
                rejection_reasons.add("performer_unresolved")
                continue
            if primary_artist not in contributor_names:
                rejection_reasons.add("performer_mismatch")
                continue
            survivors.append(entity_id)
        if len(survivors) == 1:
            return _DiscoveredCandidate(survivors[0], SubjectMatch.PROBABLE)
        if len(survivors) > 1:
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, fingerprint, _now(), ProviderOutcomeStatus.AMBIGUOUS_SUBJECT,
                                  error_category="multiple_plausible_candidates")
        return ResearchResult(
            request.request_id, "wikidata", self.policy.adapter_version, query_id, fingerprint, _now(),
            ProviderOutcomeStatus.NO_RESULT,
            error_category=(
                "version_conflict" if "version_conflict" in rejection_reasons else
                "performer_unresolved" if "performer_unresolved" in rejection_reasons else
                "performer_mismatch" if "performer_mismatch" in rejection_reasons else
                "candidate_rejected_subject_match" if rejection_reasons else "no_candidate_found"
            ),
        )

    def _entities(
        self, request: ResearchRequest, entity_ids: tuple[str, ...], query_id: str, query_fingerprint: str,
    ) -> dict[str, dict[str, object]] | ResearchResult:
        if not entity_ids:
            return {}
        params = {
            "action": "wbgetentities", "ids": "|".join(entity_ids), "languages": "all",
            "props": "labels|aliases|claims", "format": "json",
        }
        response = self._get(self.policy.api_url, params, request, query_id, query_fingerprint)
        if isinstance(response, ResearchResult):
            return response
        try:
            values = response.json()["entities"]
        except (ValueError, TypeError, KeyError):
            return self._failure(request, query_id, query_fingerprint, "invalid_response")
        return {
            entity_id: entity for entity_id, entity in values.items()
            if entity_id in entity_ids and isinstance(entity, dict) and not entity.get("missing")
        } if isinstance(values, dict) else self._failure(request, query_id, query_fingerprint, "invalid_response")

    @staticmethod
    def _entity_labels(entity: dict[str, object]) -> tuple[str, ...]:
        labels = entity.get("labels")
        if not isinstance(labels, dict):
            return ()
        return tuple(
            str(value.get("value")) for value in labels.values()
            if isinstance(value, dict) and isinstance(value.get("value"), str)
        )

    @classmethod
    def _entity_names(cls, entity: dict[str, object]) -> tuple[str, ...]:
        aliases = entity.get("aliases")
        values = list(cls._entity_labels(entity))
        if isinstance(aliases, dict):
            values.extend(
                str(alias.get("value")) for aliases_for_language in aliases.values()
                if isinstance(aliases_for_language, list)
                for alias in aliases_for_language
                if isinstance(alias, dict) and isinstance(alias.get("value"), str)
            )
        return tuple(values)

    @classmethod
    def _matches_title(cls, entity: dict[str, object], expected: str) -> bool:
        return expected in {_normalized_text(value) for value in cls._entity_names(entity)}

    @classmethod
    def _version_conflicts(cls, version_cues: tuple[str, ...], entity: dict[str, object]) -> bool:
        candidate_text = " ".join(_normalized_text(value) for value in cls._entity_names(entity))
        categories = {
            "live": ("live",),
            "remix": ("remix",),
            "acoustic": ("acoustic",),
            "instrumental": ("instrumental",),
        }
        requested = {
            category for category, terms in categories.items()
            if any(term in _normalized_text(cue).split() for cue in version_cues for term in terms)
        }
        candidate = {
            category for category, terms in categories.items()
            if any(term in candidate_text.split() for term in terms)
        }
        return bool(requested and candidate and requested != candidate)

    @staticmethod
    def _claim_entity_ids(entity: dict[str, object], property_id: str) -> tuple[str, ...]:
        claims = entity.get("claims")
        values = claims.get(property_id) if isinstance(claims, dict) else None
        return tuple(
            value["mainsnak"]["datavalue"]["value"]["id"]
            for value in values if isinstance(value, dict)
            and isinstance(value.get("mainsnak"), dict)
            and isinstance(value["mainsnak"].get("datavalue"), dict)
            and isinstance(value["mainsnak"]["datavalue"].get("value"), dict)
            and isinstance(value["mainsnak"]["datavalue"]["value"].get("id"), str)
        ) if isinstance(values, list) else ()

    def _get(self, url: str, params: dict[str, str], request: ResearchRequest,
             query_id: str, query_fingerprint: str) -> httpx.Response | ResearchResult:
        try:
            response = self.client.get(url, params=params,
                                       headers={"User-Agent": self.policy.user_agent,
                                                "Accept": "application/sparql-results+json"})
        except (httpx.TimeoutException, httpx.TransportError):
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, query_fingerprint, _now(),
                                  ProviderOutcomeStatus.TRANSIENT_ERROR,
                                  error_category="transport_error")
        retry_after = None
        try:
            retry_after = float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            pass
        if response.status_code == 429 or response.status_code >= 500:
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, query_fingerprint, _now(),
                                  ProviderOutcomeStatus.TRANSIENT_ERROR,
                                  retry_after_seconds=retry_after,
                                  error_category="provider_temporarily_unavailable")
        if response.status_code >= 400:
            return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                                  query_id, query_fingerprint, _now(), ProviderOutcomeStatus.UNAVAILABLE,
                                  error_category="provider_request_rejected")
        return response

    def _failure(self, request: ResearchRequest, query_id: str,
                 query_fingerprint: str, category: str) -> ResearchResult:
        return ResearchResult(request.request_id, "wikidata", self.policy.adapter_version,
                              query_id, query_fingerprint, _now(), ProviderOutcomeStatus.UNAVAILABLE,
                              error_category=category)

    @staticmethod
    def _binding_value(binding: dict[str, object], key: str) -> str | None:
        item = binding.get(key)
        if not isinstance(item, dict):
            return None
        value = item.get("value")
        return value if isinstance(value, str) else None

    @staticmethod
    def _statement_query(item_selector: str) -> str:
        return f"""
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX p: <http://www.wikidata.org/prop/>
        PREFIX ps: <http://www.wikidata.org/prop/statement/>
        PREFIX wikibase: <http://wikiba.se/ontology#>
        PREFIX bd: <http://www.bigdata.com/rdf#>
        SELECT ?item ?statement ?claim ?value ?valueLabel WHERE {{
          {item_selector}
          OPTIONAL {{
            VALUES (?statementProperty ?valueProperty ?claim) {{
              (p:P136 ps:P136 "genre")
              (p:P407 ps:P407 "language")
              (p:P495 ps:P495 "country")
              (p:P175 ps:P175 "performer")
              (p:P86 ps:P86 "composer")
              (p:P31 ps:P31 "instance_of")
              (p:P361 ps:P361 "part_of")
            }}
            ?item ?statementProperty ?statement .
            ?statement ?valueProperty ?value .
            SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
          }}
        }}
        """.strip()
