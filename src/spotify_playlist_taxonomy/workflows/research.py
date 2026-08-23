"""Phase 5 research coordination, normalization, caching, and sufficiency."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from spotify_playlist_taxonomy.config import ResearchPolicyDocument
from spotify_playlist_taxonomy.domain.models import ResearchEvidence
from spotify_playlist_taxonomy.domain.research import (
    EvidenceNeedAssessment,
    EvidenceType,
    ResearchRunSummary,
    SufficiencyAction,
    SufficiencyReport,
    SufficiencyState,
)
from spotify_playlist_taxonomy.persistence.research import ResearchStore
from spotify_playlist_taxonomy.ports.research import (
    IndependentIdentifier,
    ProviderDescriptor,
    ProviderOutcomeStatus,
    ResearchProvider,
    ResearchRequest,
    ResearchResult,
    SubjectMatch,
)
from spotify_playlist_taxonomy.workflows.research_grounding import CanonicalExternalSubjectResolver


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def subject_fingerprint(
    *, canonical_track_id: str, title: str, artists: tuple[str, ...],
    version_cues: tuple[str, ...], identifiers: tuple[IndependentIdentifier, ...],
) -> str:
    """Version-sensitive identity digest; never a matching or merging rule."""

    return _digest({
        "schema": "research-subject.v1",
        "canonical_track_id": canonical_track_id,
        "title": title,
        "artists": list(artists),
        "version_cues": list(version_cues),
        "independent_identifiers": [
            {"namespace": item.namespace, "value": item.value}
            for item in sorted(identifiers, key=lambda item: (item.namespace, item.value))
        ],
    })


class EvidenceNormalizer:
    def __init__(self, *, schema_version: str, normalizer_version: str):
        self.schema_version = schema_version
        self.normalizer_version = normalizer_version

    def normalize(
        self, *, request: ResearchRequest, result: ResearchResult,
        assertion_ids: tuple[str, ...], fresh_until: str,
    ) -> tuple[ResearchEvidence, ...]:
        if result.status is not ProviderOutcomeStatus.SUCCESS:
            return ()
        if len(assertion_ids) != len(result.assertions):
            raise ValueError("assertion ID count mismatch")
        output: list[ResearchEvidence] = []
        for assertion_id, assertion in zip(assertion_ids, result.assertions, strict=True):
            claim = assertion.asserted_value.get("claim")
            if not isinstance(claim, str) or not claim.strip():
                raise ValueError("provider assertion requires a controlled claim key")
            normalized = _canonical_json(assertion.asserted_value)
            content_digest = _digest({
                "schema": self.schema_version,
                "canonical_track_id": request.canonical_track_id,
                "subject_fingerprint": request.subject_fingerprint,
                "provider_id": result.provider_id,
                "provider_version": result.adapter_version,
                "assertion_key": assertion.assertion_key,
                "evidence_type": assertion.evidence_type.value,
                "value": assertion.asserted_value,
            })
            output.append(ResearchEvidence(
                evidence_id=f"evidence.{content_digest[:32]}",
                canonical_track_id=request.canonical_track_id,
                source=assertion.source,
                kind=assertion.kind,
                normalized_value=normalized,
                source_reference=assertion.source_reference,
                confidence=assertion.native_confidence,
                provider_assertion_id=assertion_id,
                provider_id=result.provider_id,
                provider_version=result.adapter_version,
                request_id=request.request_id,
                query_id=result.query_id,
                subject_fingerprint=request.subject_fingerprint,
                subject_scope=assertion.subject_scope.value,
                subject_match=assertion.subject_match.value,
                evidence_type=assertion.evidence_type.value,
                claim_key=claim,
                quality_band=assertion.quality_band.value,
                normalized_schema_version=self.schema_version,
                normalizer_version=self.normalizer_version,
                retrieved_at=result.retrieved_at,
                fresh_until=fresh_until,
                evidence_state="active",
                content_digest=content_digest,
            ))
        return tuple(output)


class EvidenceSufficiencyService:
    def __init__(
        self, *, store: ResearchStore, descriptors: tuple[ProviderDescriptor, ...],
        phase4_bundle_digest: str,
    ):
        self.store = store
        self.descriptors = descriptors
        self.phase4_bundle_digest = phase4_bundle_digest

    def assess(self, canonical_track_id: str, *, now: str) -> SufficiencyReport:
        evidence = self.store.evidence_for_track(canonical_track_id)
        attempts = self.store.attempts_for_track(canonical_track_id)
        assessments = tuple(
            self._assess_type(kind, evidence, attempts, now)
            for kind in EvidenceType
        )
        return SufficiencyReport(canonical_track_id, self.phase4_bundle_digest, assessments)

    def _assess_type(
        self, evidence_type: EvidenceType, evidence: tuple[ResearchEvidence, ...],
        attempts: tuple[object, ...], now: str,
    ) -> EvidenceNeedAssessment:
        relevant = tuple(item for item in evidence if item.evidence_type == evidence_type.value)
        active = tuple(
            item for item in relevant
            if item.evidence_state == "active"
            and (item.fresh_until is None or _parse_iso(item.fresh_until) > _parse_iso(now))
        )
        stale = tuple(
            item for item in relevant
            if item.evidence_state == "stale"
            or (
                item.evidence_state == "active" and item.fresh_until is not None
                and _parse_iso(item.fresh_until) <= _parse_iso(now)
            )
        )
        exact = tuple(item for item in active if item.subject_match == SubjectMatch.EXACT.value)
        contextual = tuple(item for item in active if item.subject_match != SubjectMatch.EXACT.value)
        capable = {
            item.provider_id for item in self.descriptors
            if evidence_type in item.supported_evidence_types
        }
        provider_ids = tuple(sorted({
            str(item["provider_id"]) for item in attempts
            if str(item["provider_id"]) in capable
        }))

        state: SufficiencyState
        reasons: tuple[str, ...]
        if exact and any(
            json.loads(item.normalized_value).get("not_applicable") is True for item in exact
        ):
            state, reasons = SufficiencyState.NOT_APPLICABLE, ("explicit_not_applicable",)
        elif self._has_contradiction(exact):
            state, reasons = SufficiencyState.CONTRADICTORY, ("independent_values_disagree",)
        elif exact and any(item.quality_band in {"high", "medium"} for item in exact):
            state, reasons = SufficiencyState.SATISFIED, ("current_exact_evidence",)
        elif exact:
            state, reasons = SufficiencyState.LOW_QUALITY, ("exact_evidence_below_quality_floor",)
        elif contextual:
            state, reasons = SufficiencyState.SUBJECT_AMBIGUOUS, ("no_exact_recording_evidence",)
        elif stale:
            state, reasons = SufficiencyState.STALE, ("only_stale_evidence",)
        elif not provider_ids:
            state, reasons = SufficiencyState.NOT_ASSESSED, ("no_provider_attempt",)
        else:
            outcomes = {str(item["outcome"]) for item in attempts if str(item["provider_id"]) in capable}
            if outcomes and outcomes <= {"transient_error", "unavailable"}:
                state, reasons = SufficiencyState.PROVIDER_UNAVAILABLE, ("capable_providers_unavailable",)
            else:
                state, reasons = SufficiencyState.MISSING, ("queried_without_usable_evidence",)

        consulted_capable = set(provider_ids) & capable
        unconsulted = capable - consulted_capable
        if state in {SufficiencyState.SATISFIED, SufficiencyState.NOT_APPLICABLE}:
            action = SufficiencyAction.NONE
        elif state in {SufficiencyState.CONTRADICTORY, SufficiencyState.SUBJECT_AMBIGUOUS}:
            action = SufficiencyAction.HUMAN_REVIEW
        elif state is SufficiencyState.STALE:
            action = SufficiencyAction.REFRESH
        elif unconsulted:
            action = SufficiencyAction.QUERY_PROVIDER
        elif state is SufficiencyState.PROVIDER_UNAVAILABLE:
            action = SufficiencyAction.REFRESH
        else:
            action = SufficiencyAction.HUMAN_REVIEW
        return EvidenceNeedAssessment(
            evidence_type, state, tuple(item.evidence_id for item in relevant),
            provider_ids, reasons, action,
        )

    @staticmethod
    def _has_contradiction(evidence: tuple[ResearchEvidence, ...]) -> bool:
        grouped: dict[str, set[str]] = {}
        lineages: dict[str, set[str]] = {}
        for item in evidence:
            if item.claim_key is None:
                continue
            grouped.setdefault(item.claim_key, set()).add(item.normalized_value)
            lineages.setdefault(item.claim_key, set()).add(item.provider_id or item.source.value)
        return any(len(grouped[key]) > 1 and len(lineages[key]) > 1 for key in grouped)


class ResearchActionabilityService:
    """Read-only projection of Phase 5 provider work currently due."""

    def __init__(
        self, sufficiency: EvidenceSufficiencyService,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.sufficiency = sufficiency
        self.now = now or (lambda: datetime.now(UTC))

    def project(self, canonical_track_ids: tuple[str, ...]) -> ResearchActionabilityProjection:
        timestamp = _iso(self.now())
        actionable: set[str] = set()
        refresh: set[str] = set()
        for canonical_track_id in canonical_track_ids:
            actions = {
                item.next_action
                for item in self.sufficiency.assess(canonical_track_id, now=timestamp).assessments
            }
            if actions & {SufficiencyAction.QUERY_PROVIDER, SufficiencyAction.REFRESH}:
                actionable.add(canonical_track_id)
            if SufficiencyAction.REFRESH in actions:
                refresh.add(canonical_track_id)
        return ResearchActionabilityProjection(frozenset(actionable), frozenset(refresh))


@dataclass(frozen=True, slots=True)
class ResearchActionabilityProjection:
    actionable_canonical_ids: frozenset[str]
    refresh_canonical_ids: frozenset[str]


class ResearchCoordinator:
    def __init__(
        self, *, store: ResearchStore, providers: Iterable[ResearchProvider],
        policy: ResearchPolicyDocument, phase4_bundle_digest: str,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        subject_resolver: CanonicalExternalSubjectResolver | None = None,
    ):
        self.store = store
        self.providers = {item.descriptor.provider_id: item for item in providers}
        self.policy = policy
        self.phase4_bundle_digest = phase4_bundle_digest
        self.now = now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        self.sleep = sleep
        self.subject_resolver = subject_resolver or CanonicalExternalSubjectResolver(
            store.connection, now=self.now,
        )
        self._last_call: dict[str, float] = {}
        self.normalizer = EvidenceNormalizer(
            schema_version=policy.normalized_evidence_schema_version,
            normalizer_version=policy.normalizer_version,
        )

    def run(
        self, canonical_track_ids: tuple[str, ...], *, provider_id: str | None = None,
        refresh: bool = False, limit: int | None = None,
    ) -> ResearchRunSummary:
        selected_limit = self.policy.default_limit if limit is None else limit
        if not canonical_track_ids:
            raise ValueError("at least one explicit canonical ID is required")
        if selected_limit < 1 or selected_limit > self.policy.hard_limit:
            raise ValueError(f"limit must be between 1 and {self.policy.hard_limit}")
        if len(canonical_track_ids) > selected_limit:
            raise ValueError("explicit canonical ID count exceeds the selected limit")
        if len(canonical_track_ids) != len(set(canonical_track_ids)):
            raise ValueError("canonical IDs must be unique")
        selected = self._selected_providers(provider_id)
        job_ids: list[str] = []
        provider_calls = cache_hits = evidence_created = 0
        for canonical_track_id in canonical_track_ids:
            subject = self.store.subject(canonical_track_id)
            identifiers = list(subject.independent_identifiers)
            grounding = self.subject_resolver.resolve(canonical_track_id)
            if grounding.is_exact and grounding.musicbrainz_recording_id is not None:
                identifiers.append(IndependentIdentifier(
                    "musicbrainz_recording", grounding.musicbrainz_recording_id,
                ))
            identifiers = list({(item.namespace, item.value): item for item in identifiers}.values())
            fingerprint = subject_fingerprint(
                canonical_track_id=canonical_track_id,
                title=subject.track.display_title,
                artists=subject.track.display_artists,
                version_cues=subject.version_cues,
                identifiers=tuple(identifiers),
            )
            requested_at = _iso(self.now())
            job_id = f"research-job.{uuid.uuid4().hex}"
            request_id = f"research-request.{uuid.uuid4().hex}"
            job_ids.append(job_id)
            self.store.create_job(
                job_id=job_id, request_id=request_id, canonical_track_id=canonical_track_id,
                subject_fingerprint=fingerprint, policy_version=self.policy.policy_version,
                bundle_digest=self.phase4_bundle_digest, requested_at=requested_at,
            )
            outcomes: list[ProviderOutcomeStatus] = []
            for provider in selected:
                supported = provider.descriptor.supported_evidence_types
                request = ResearchRequest(
                    request_id, self.policy.request_schema_version, canonical_track_id,
                    fingerprint, subject.track.display_title, subject.track.display_artists,
                    subject.version_cues, tuple(identifiers), supported, requested_at,
                )
                cache_key = self._cache_key(provider.descriptor, request)
                cached = None if refresh else self.store.fresh_cache(cache_key, _iso(self.now()))
                if cached is not None:
                    result = self.store.cached_result(cached, request_id, _iso(self.now()))
                    self.store.record_cache_hit(
                        attempt_id=f"research-attempt.{uuid.uuid4().hex}", job_id=job_id,
                        result=result, cached_from_attempt_id=cached.producing_attempt_id,
                        timestamp=_iso(self.now()),
                    )
                    cache_hits += 1
                else:
                    result, calls, created = self._call_provider(
                        provider, request, job_id, cache_key, fingerprint,
                    )
                    provider_calls += calls
                    evidence_created += created
                outcomes.append(result.status)
                if result.status is ProviderOutcomeStatus.SUCCESS:
                    identifiers.extend(self._independent_identifiers(result))
            success_like = {
                ProviderOutcomeStatus.SUCCESS, ProviderOutcomeStatus.NO_RESULT,
                ProviderOutcomeStatus.AMBIGUOUS_SUBJECT, ProviderOutcomeStatus.UNSUPPORTED,
            }
            if outcomes and all(item in success_like for item in outcomes):
                status = "completed"
            elif any(item in success_like for item in outcomes):
                status = "partial"
            else:
                status = "failed"
            self.store.finish_job(job_id, status, _iso(self.now()))
        return ResearchRunSummary(tuple(job_ids), provider_calls, cache_hits, evidence_created)

    def _selected_providers(self, provider_id: str | None) -> tuple[ResearchProvider, ...]:
        configured = [item for item in self.policy.providers if item.enabled]
        if provider_id is not None:
            configured = [item for item in configured if item.provider_id == provider_id]
            if not configured:
                raise ValueError("requested research provider is not enabled")
        missing = [item.provider_id for item in configured if item.provider_id not in self.providers]
        if missing:
            raise ValueError("enabled research provider adapter is unavailable: " + ", ".join(missing))
        return tuple(self.providers[item.provider_id] for item in configured)

    def _call_provider(
        self, provider: ResearchProvider, request: ResearchRequest, job_id: str,
        cache_key: str, subject_fingerprint_value: str,
    ) -> tuple[ResearchResult, int, int]:
        calls = 0
        for attempt_number in range(1, self.policy.max_attempts + 1):
            self._throttle(provider.descriptor)
            started_at = _iso(self.now())
            try:
                result = provider.research(request)
            except Exception:
                result = ResearchResult(
                    request.request_id, provider.descriptor.provider_id,
                    provider.descriptor.adapter_version,
                    f"failed:{request.request_id}:{attempt_number}",
                    _digest({"provider": provider.descriptor.provider_id, "request": request.request_id}),
                    _iso(self.now()), ProviderOutcomeStatus.UNAVAILABLE,
                    error_category="provider_adapter_error",
                )
            calls += 1
            attempt_id = f"research-attempt.{uuid.uuid4().hex}"
            assertion_ids = tuple(
                f"research-assertion.{uuid.uuid4().hex}" for _ in result.assertions
            )
            fresh_until, negative_until, persist_cache = self._expiry(result)
            evidence = self.normalizer.normalize(
                request=request, result=result, assertion_ids=assertion_ids,
                fresh_until=fresh_until or _iso(self.now()),
            )
            self.store.record_attempt(
                attempt_id=attempt_id, job_id=job_id,
                canonical_track_id=request.canonical_track_id,
                subject_fingerprint=subject_fingerprint_value, result=result,
                assertion_ids=assertion_ids, evidences=evidence, started_at=started_at,
                attempt_number=attempt_number,
                cache_key=cache_key if persist_cache else None,
                request_schema_version=request.schema_version,
                fresh_until=fresh_until, negative_until=negative_until,
            )
            if result.status is not ProviderOutcomeStatus.TRANSIENT_ERROR:
                return result, calls, len(evidence)
            if attempt_number < self.policy.max_attempts:
                delay = result.retry_after_seconds
                if delay is None:
                    delay = float(2 ** (attempt_number - 1))
                self.sleep(delay)
        return result, calls, 0

    def _expiry(self, result: ResearchResult) -> tuple[str | None, str | None, bool]:
        retrieved = _parse_iso(result.retrieved_at)
        if result.status is ProviderOutcomeStatus.SUCCESS:
            return _iso(retrieved + timedelta(hours=self.policy.positive_ttl_hours)), None, True
        if result.status is ProviderOutcomeStatus.NO_RESULT:
            return None, _iso(retrieved + timedelta(hours=self.policy.negative_ttl_hours)), True
        if result.status is ProviderOutcomeStatus.AMBIGUOUS_SUBJECT:
            return None, _iso(retrieved + timedelta(hours=self.policy.ambiguous_ttl_hours)), True
        if result.status is ProviderOutcomeStatus.UNSUPPORTED:
            return None, None, True
        return None, None, False

    def _cache_key(self, descriptor: ProviderDescriptor, request: ResearchRequest) -> str:
        return _digest({
            "cache_schema": "research-cache.v1",
            "provider_id": descriptor.provider_id,
            "adapter_version": descriptor.adapter_version,
            "request_schema_version": request.schema_version,
            "canonical_track_id": request.canonical_track_id,
            "subject_fingerprint": request.subject_fingerprint,
            "requested_evidence_types": [item.value for item in request.requested_evidence_types],
            "identifiers": [
                {"namespace": item.namespace, "value": item.value}
                for item in request.independent_identifiers
            ],
        })

    def _throttle(self, descriptor: ProviderDescriptor) -> None:
        previous = self._last_call.get(descriptor.provider_id)
        current = self.monotonic()
        if previous is not None:
            remaining = descriptor.minimum_interval_seconds - (current - previous)
            if remaining > 0:
                self.sleep(remaining)
        self._last_call[descriptor.provider_id] = self.monotonic()

    @staticmethod
    def _independent_identifiers(result: ResearchResult) -> tuple[IndependentIdentifier, ...]:
        output: list[IndependentIdentifier] = []
        for assertion in result.assertions:
            if assertion.evidence_type is not EvidenceType.INDEPENDENT_IDENTIFIER:
                continue
            if assertion.subject_match is not SubjectMatch.EXACT:
                continue
            namespace = assertion.asserted_value.get("namespace")
            value = assertion.asserted_value.get("value")
            if isinstance(namespace, str) and isinstance(value, str):
                output.append(IndependentIdentifier(namespace, value))
        return tuple(output)
