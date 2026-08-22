"""Conservative MusicBrainz recording evidence adapter."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
import time
from typing import Callable

import httpx

from spotify_playlist_taxonomy.config import ResearchProviderPolicy
from spotify_playlist_taxonomy.domain.models import EvidenceKind, ResearchSource
from spotify_playlist_taxonomy.domain.research import EvidenceType, QualityBand, SubjectMatch, SubjectScope
from spotify_playlist_taxonomy.ports.research import (
    ProviderAssertion, ProviderDescriptor, ProviderOutcomeStatus,
    ResearchRequest, ResearchResult,
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


_MBID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


@dataclass(frozen=True, slots=True)
class MusicBrainzRecordingLookupResult:
    normalized_isrc: str
    recording_ids: tuple[str, ...]
    query_fingerprint: str
    adapter_version: str
    retrieved_at: str
    failure_category: str | None = None


class MusicBrainzRequestGate:
    """Serial, monotonic request pacing shared by MusicBrainz adapter instances."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._monotonic, self._sleep = monotonic, sleep
        self._lock = Lock()
        self._last_request_started: float | None = None

    def wait_for_turn(self, minimum_interval_seconds: float) -> None:
        with self._lock:
            now = self._monotonic()
            if self._last_request_started is not None:
                remaining = minimum_interval_seconds - (now - self._last_request_started)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_started = self._monotonic()


_PROCESS_MUSICBRAINZ_REQUEST_GATE = MusicBrainzRequestGate()


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


class MusicBrainzResearchProvider:
    def __init__(self, *, client: httpx.Client, policy: ResearchProviderPolicy,
                 request_gate: MusicBrainzRequestGate | None = None,
                 lookup_max_attempts: int = 1,
                 sleep: Callable[[float], None] = time.sleep):
        if policy.provider_id != "musicbrainz":
            raise ValueError("MusicBrainz adapter requires musicbrainz policy")
        self.client = client
        self.policy = policy
        if not 1 <= lookup_max_attempts <= 3:
            raise ValueError("lookup_max_attempts must be between 1 and 3")
        self._request_gate = request_gate or _PROCESS_MUSICBRAINZ_REQUEST_GATE
        self._lookup_max_attempts, self._sleep = lookup_max_attempts, sleep
        self._descriptor = ProviderDescriptor(
            "musicbrainz", policy.adapter_version, "research-provider.v1",
            (
                EvidenceType.RECORDING_IDENTITY, EvidenceType.VERSION_INFORMATION,
                EvidenceType.INDEPENDENT_IDENTIFIER, EvidenceType.RELEASE,
                EvidenceType.CREDITS, EvidenceType.RELATIONSHIP, EvidenceType.WORK_CONTEXT,
            ), False, policy.minimum_interval_seconds,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def research(self, request: ResearchRequest) -> ResearchResult:
        mbid = next((item.value for item in request.independent_identifiers
                     if item.namespace in {"musicbrainz", "musicbrainz_recording"}), None)
        if mbid and re.fullmatch(r"[0-9a-fA-F-]{36}", mbid):
            path = f"{self.policy.api_url.rstrip('/')}/recording/{mbid}"
            params = {"fmt": "json", "inc": "artist-credits+releases+work-rels+artist-rels"}
            match = SubjectMatch.EXACT
        else:
            path = f"{self.policy.api_url.rstrip('/')}/recording"
            escaped_title = request.title.replace('"', "")
            escaped_artist = request.artists[0].replace('"', "")
            params = {"query": f'recording:"{escaped_title}" AND artist:"{escaped_artist}"',
                      "fmt": "json", "limit": "5"}
            match = SubjectMatch.PROBABLE
        query_fingerprint = _fingerprint({"url": path, "params": params})
        query_id = f"musicbrainz-query.{query_fingerprint[:24]}"
        response = self._request(path, params)
        if isinstance(response, ResearchResult):
            return ResearchResult(request.request_id, "musicbrainz", self.policy.adapter_version,
                                  query_id, query_fingerprint, response.retrieved_at, response.status,
                                  retry_after_seconds=response.retry_after_seconds,
                                  error_category=response.error_category)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return self._failure(request, query_id, query_fingerprint, "invalid_response")
        if not isinstance(payload, dict):
            return self._failure(request, query_id, query_fingerprint, "invalid_response")
        if mbid:
            recording = payload
        else:
            candidates = payload.get("recordings")
            if not isinstance(candidates, list):
                return self._failure(request, query_id, query_fingerprint, "invalid_response")
            matching = [item for item in candidates if self._title_artist_match(item, request)]
            if not matching:
                return ResearchResult(request.request_id, "musicbrainz", self.policy.adapter_version,
                                      query_id, query_fingerprint, _now(), ProviderOutcomeStatus.NO_RESULT)
            if len(matching) != 1:
                return ResearchResult(request.request_id, "musicbrainz", self.policy.adapter_version,
                                      query_id, query_fingerprint, _now(),
                                      ProviderOutcomeStatus.AMBIGUOUS_SUBJECT)
            recording = matching[0]
        assertions = self._assertions(recording, match)
        if not assertions:
            return ResearchResult(request.request_id, "musicbrainz", self.policy.adapter_version,
                                  query_id, query_fingerprint, _now(), ProviderOutcomeStatus.NO_RESULT)
        return ResearchResult(request.request_id, "musicbrainz", self.policy.adapter_version,
                              query_id, query_fingerprint, _now(), ProviderOutcomeStatus.SUCCESS,
                              assertions)

    def lookup_recordings_by_isrc(self, isrc: str) -> MusicBrainzRecordingLookupResult:
        """Return only normalized Recording IDs for one ISRC relationship lookup."""
        normalized_isrc = isrc.strip().replace("-", "").upper()
        if not _ISRC.fullmatch(normalized_isrc):
            raise ValueError("ISRC must be normalized before MusicBrainz lookup")
        path = f"{self.policy.api_url.rstrip('/')}/isrc/{normalized_isrc}"
        params = {"fmt": "json"}
        fingerprint = _fingerprint({"url": path, "params": params})
        response: httpx.Response | ResearchResult
        for attempt_number in range(1, self._lookup_max_attempts + 1):
            response = self._request(path, params)
            if not isinstance(response, ResearchResult) or response.status is not ProviderOutcomeStatus.TRANSIENT_ERROR:
                break
            if attempt_number < self._lookup_max_attempts:
                self._sleep(response.retry_after_seconds if response.retry_after_seconds is not None else float(2 ** (attempt_number - 1)))
        if isinstance(response, ResearchResult):
            category = response.error_category
            if response.status is ProviderOutcomeStatus.NO_RESULT:
                return MusicBrainzRecordingLookupResult(normalized_isrc, (), fingerprint, self.policy.adapter_version, response.retrieved_at)
            return MusicBrainzRecordingLookupResult(normalized_isrc, (), fingerprint, self.policy.adapter_version, response.retrieved_at, category or "provider_failure")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return MusicBrainzRecordingLookupResult(normalized_isrc, (), fingerprint, self.policy.adapter_version, _now(), "invalid_response")
        recordings = payload.get("recordings") if isinstance(payload, dict) else None
        if not isinstance(recordings, list):
            return MusicBrainzRecordingLookupResult(normalized_isrc, (), fingerprint, self.policy.adapter_version, _now(), "invalid_response")
        values: set[str] = set()
        for recording in recordings:
            value = recording.get("id") if isinstance(recording, dict) else None
            if not isinstance(value, str) or not _MBID.fullmatch(value):
                return MusicBrainzRecordingLookupResult(normalized_isrc, (), fingerprint, self.policy.adapter_version, _now(), "invalid_response")
            values.add(value.lower())
        return MusicBrainzRecordingLookupResult(normalized_isrc, tuple(sorted(values)), fingerprint, self.policy.adapter_version, _now())

    def _request(self, url: str, params: dict[str, str]) -> httpx.Response | ResearchResult:
        self._request_gate.wait_for_turn(self.policy.minimum_interval_seconds)
        try:
            response = self.client.get(url, params=params,
                                       headers={"User-Agent": self.policy.user_agent,
                                                "Accept": "application/json"})
        except (httpx.TimeoutException, httpx.TransportError):
            return ResearchResult("transport", "musicbrainz", self.policy.adapter_version,
                                  "transport", "transport", _now(),
                                  ProviderOutcomeStatus.TRANSIENT_ERROR,
                                  error_category="transport_error")
        if response.status_code == 429 or response.status_code >= 500:
            return ResearchResult("http", "musicbrainz", self.policy.adapter_version,
                                  "http", "http", _now(), ProviderOutcomeStatus.TRANSIENT_ERROR,
                                  retry_after_seconds=_retry_after(response),
                                  error_category="provider_temporarily_unavailable")
        if response.status_code == 404:
            return ResearchResult("http", "musicbrainz", self.policy.adapter_version,
                                  "http", "http", _now(), ProviderOutcomeStatus.NO_RESULT)
        if response.status_code >= 400:
            return ResearchResult("http", "musicbrainz", self.policy.adapter_version,
                                  "http", "http", _now(), ProviderOutcomeStatus.UNAVAILABLE,
                                  error_category="provider_request_rejected")
        return response

    def _failure(self, request: ResearchRequest, query_id: str,
                 query_fingerprint: str, category: str) -> ResearchResult:
        return ResearchResult(request.request_id, "musicbrainz", self.policy.adapter_version,
                              query_id, query_fingerprint, _now(), ProviderOutcomeStatus.UNAVAILABLE,
                              error_category=category)

    @staticmethod
    def _title_artist_match(item: object, request: ResearchRequest) -> bool:
        if not isinstance(item, dict) or _normalized(str(item.get("title", ""))) != _normalized(request.title):
            return False
        credits = item.get("artist-credit")
        if not isinstance(credits, list):
            return False
        names = tuple(str(value.get("name") or value.get("artist", {}).get("name", ""))
                      for value in credits if isinstance(value, dict))
        return tuple(map(_normalized, names)) == tuple(map(_normalized, request.artists))

    def _assertions(self, recording: dict[str, object],
                    match: SubjectMatch) -> tuple[ProviderAssertion, ...]:
        recording_id = recording.get("id")
        if not isinstance(recording_id, str) or not recording_id:
            return ()
        reference = f"https://musicbrainz.org/recording/{recording_id}"
        quality = QualityBand.HIGH if match is SubjectMatch.EXACT else QualityBand.MEDIUM
        values: list[tuple[str, EvidenceType, dict[str, object], SubjectScope]] = [
            ("recording_id", EvidenceType.INDEPENDENT_IDENTIFIER,
             {"claim": "musicbrainz_recording_id", "namespace": "musicbrainz_recording",
              "value": recording_id}, SubjectScope.RECORDING),
            ("recording_title", EvidenceType.RECORDING_IDENTITY,
             {"claim": "recording_title", "value": str(recording.get("title", ""))},
             SubjectScope.RECORDING),
        ]
        disambiguation = recording.get("disambiguation")
        if isinstance(disambiguation, str) and disambiguation.strip():
            values.append(("version", EvidenceType.VERSION_INFORMATION,
                           {"claim": "recording_disambiguation", "value": disambiguation.strip()},
                           SubjectScope.RECORDING))
        credits = recording.get("artist-credit")
        if isinstance(credits, list):
            names = [str(item.get("name") or item.get("artist", {}).get("name", ""))
                     for item in credits if isinstance(item, dict)]
            names = [name for name in names if name]
            if names:
                values.append(("credits", EvidenceType.CREDITS,
                               {"claim": "recording_artist_credit", "values": names},
                               SubjectScope.RECORDING))
        releases = recording.get("releases")
        if isinstance(releases, list):
            release_ids = sorted({str(item["id"]) for item in releases
                                  if isinstance(item, dict) and item.get("id")})
            if release_ids:
                values.append(("releases", EvidenceType.RELEASE,
                               {"claim": "musicbrainz_release_ids", "values": release_ids},
                               SubjectScope.RECORDING))
        relations = recording.get("relations")
        if isinstance(relations, list):
            work_ids = sorted({str(item["work"]["id"]) for item in relations
                               if isinstance(item, dict) and isinstance(item.get("work"), dict)
                               and item["work"].get("id")})
            if work_ids:
                values.append(("works", EvidenceType.WORK_CONTEXT,
                               {"claim": "musicbrainz_related_work_ids", "values": work_ids},
                               SubjectScope.WORK))
            relationship_types = sorted({
                str(item["type"]) for item in relations
                if isinstance(item, dict) and item.get("type")
            })
            if relationship_types:
                values.append((
                    "relationships", EvidenceType.RELATIONSHIP,
                    {"claim": "musicbrainz_recording_relationship_types",
                     "values": relationship_types}, SubjectScope.RECORDING,
                ))
        return tuple(ProviderAssertion(
            key, evidence_type, value, EvidenceKind.SOURCED_FACT, ResearchSource.MUSICBRAINZ,
            reference, scope, match if scope is SubjectScope.RECORDING else SubjectMatch.CONTEXTUAL,
            quality_band=quality,
        ) for key, evidence_type, value, scope in values if value.get("value") != "")
