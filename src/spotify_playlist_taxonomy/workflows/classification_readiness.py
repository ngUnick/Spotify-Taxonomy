"""Read-only aggregate readiness projection for supervised catalogue classification."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.workflows.phase6 import Phase6Service
from spotify_playlist_taxonomy.workflows.phase7 import EffectiveReview, Phase7Service
from spotify_playlist_taxonomy.workflows.phase9 import DesiredState, Phase9Service
from spotify_playlist_taxonomy.workflows.phase11 import (
    current_account_canonical_identity_mappings,
    exact_spotify_population_mappings,
    is_exact_spotify_population_mapping,
)
from spotify_playlist_taxonomy.workflows.genre_routing import GenreRoutingService
from spotify_playlist_taxonomy.workflows.research import ResearchActionabilityService


class ReadinessBucket(StrEnum):
    POPULATION_READY = "population_ready"
    LOCAL_CLASSIFICATION_READY = "local_classification_ready"
    RESEARCH_NEEDED = "research_needed"
    REVIEW_BLOCKED = "review_blocked"
    POPULATION_MAPPING_BLOCKED = "population_mapping_blocked"
    NO_ACTIONABLE_CLASSIFICATION = "no_actionable_classification"


@dataclass(frozen=True, slots=True)
class ReadinessSelection:
    canonical_track_id: str
    bucket: ReadinessBucket
    next_action: str
    desired_target_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClassificationReadinessReport:
    canonical_tracks_total: int
    classification_eligible_canonicals: int
    spotify_population_eligible_canonicals: int
    spotify_mapping_exactly_one_safe_canonicals: int
    spotify_mapping_zero_current_canonicals: int
    spotify_mapping_multiple_current_canonicals: int
    spotify_mapping_account_context_unavailable_canonicals: int
    spotify_mapping_provider_mismatch_canonicals: int
    spotify_mapping_invalid_canonicals: int
    canonicals_with_any_current_account_link: int
    canonicals_with_current_spotify_link: int
    qualifying_research_evidence_canonicals: int
    derivable_research_evidence_canonicals: int
    research_actionable_canonicals: int
    current_derived_characteristics_canonicals: int
    current_classification_proposals_canonicals: int
    effective_include_canonicals: int
    effective_exclude_canonicals: int
    effective_abstain_canonicals: int
    review_required_canonicals: int
    genre_routing_unresolved_canonicals: int
    canonicals_with_desired_membership: int
    desired_memberships_total: int
    population_ready_canonicals: int
    local_classification_ready_canonicals: int
    research_needed_canonicals: int
    research_needed_canonical_ids: tuple[str, ...]
    review_blocked_canonicals: int
    population_mapping_blocked_canonicals: int
    selected_action: str
    selections: tuple[ReadinessSelection, ...]
    target_distribution: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ClassificationWorklistItem:
    canonical_track_id: str
    title: str
    artists: tuple[str, ...]
    population_mapping_state: str
    explicit_review_exists: bool
    explicit_genre_routing_state: str


class ClassificationReadinessService:
    """Projects existing Phase 5-9 state only; it owns no mutation or provider dependency."""

    def __init__(
        self, connection: sqlite3.Connection, bundle: Phase4Bundle,
        research_actionability: ResearchActionabilityService,
    ) -> None:
        self.connection = connection
        self.bundle = bundle
        self.phase6 = Phase6Service(connection, bundle)
        self.review = Phase7Service(connection, bundle)
        self.phase9 = Phase9Service(connection, bundle)
        self.research_actionability = research_actionability

    def report(self, limit: int = 10) -> ClassificationReadinessReport:
        if not 1 <= limit <= 25:
            raise ValueError("readiness limit must be between 1 and 25")
        canonical_ids = tuple(
            row[0] for row in self.connection.execute(
                "SELECT canonical_track_id FROM canonical_tracks ORDER BY canonical_track_id"
            )
        )
        qualifying = {row["canonical_track_id"] for row in self.phase6.qualifying_evidence()}
        derivable = self.phase6.derivable_canonical_ids()
        research_plan = self.research_actionability.project(canonical_ids)
        research_actionable = research_plan.actionable_canonical_ids
        current_characteristics = {
            row[0] for row in self.connection.execute(
                "SELECT DISTINCT canonical_track_id FROM phase6_characteristic_derivations WHERE state='current'"
            )
        }
        current_derivations = {
            row[0] for row in self.connection.execute(
                "SELECT DISTINCT canonical_track_id FROM phase6_characteristic_derivations "
                "WHERE state IN ('current', 'conflicting')"
            )
        }
        proposal_canonicals = {
            row[0] for row in self.connection.execute(
                "SELECT canonical_track_id FROM phase6_proposals UNION SELECT canonical_track_id FROM phase8_codex_proposals"
            )
        }
        effective = self.review.effective_many(canonical_ids)
        effective_by_canonical: dict[str, list[EffectiveReview]] = {key: [] for key in canonical_ids}
        for value in effective.values():
            effective_by_canonical.setdefault(value.canonical_track_id, []).append(value)
        desired_states = self.phase9.desired_many(canonical_ids)
        desired_by_canonical = {item.canonical_track_id: item for item in desired_states}
        active_binding = self.connection.execute(
            "SELECT binding_id FROM platform_account_bindings WHERE active=1 ORDER BY bound_at"
        ).fetchone()
        current_mappings = {} if active_binding is None else current_account_canonical_identity_mappings(
            self.connection, active_binding["binding_id"], canonical_ids,
        )
        mappings = {} if active_binding is None else exact_spotify_population_mappings(
            self.connection, active_binding["binding_id"], canonical_ids,
        )
        population_eligible = {
            canonical_id for canonical_id in canonical_ids
            if is_exact_spotify_population_mapping(mappings.get(canonical_id, ()))
        }
        mapping_counts = self._mapping_counts(canonical_ids, current_mappings, mappings, active_binding is None)
        distribution = {target.output_target_id: 0 for target in self.bundle.output_targets.targets}
        for state in desired_states:
            for membership in state.memberships:
                distribution[membership.output_target_id] += 1

        buckets: dict[str, ReadinessSelection] = {}
        for canonical_id in canonical_ids:
            reviews = effective_by_canonical[canonical_id]
            desired = desired_by_canonical[canonical_id]
            buckets[canonical_id] = self._bucket(
                canonical_id, desired, reviews, canonical_id in population_eligible,
                canonical_id in qualifying, canonical_id in derivable,
                canonical_id in current_derivations,
                canonical_id in proposal_canonicals, canonical_id in research_actionable,
                canonical_id in research_plan.refresh_canonical_ids,
            )

        selected_action, selected = self._select(buckets, limit)
        values = tuple(buckets.values())
        return ClassificationReadinessReport(
            canonical_tracks_total=len(canonical_ids),
            classification_eligible_canonicals=len(canonical_ids),
            spotify_population_eligible_canonicals=len(population_eligible),
            spotify_mapping_exactly_one_safe_canonicals=mapping_counts["safe"],
            spotify_mapping_zero_current_canonicals=mapping_counts["zero"],
            spotify_mapping_multiple_current_canonicals=mapping_counts["multiple"],
            spotify_mapping_account_context_unavailable_canonicals=mapping_counts["context_unavailable"],
            spotify_mapping_provider_mismatch_canonicals=mapping_counts["provider_mismatch"],
            spotify_mapping_invalid_canonicals=mapping_counts["invalid"],
            canonicals_with_any_current_account_link=mapping_counts["any_current"],
            canonicals_with_current_spotify_link=mapping_counts["current_spotify"],
            qualifying_research_evidence_canonicals=len(qualifying),
            derivable_research_evidence_canonicals=len(derivable),
            research_actionable_canonicals=len(research_actionable),
            current_derived_characteristics_canonicals=len(current_characteristics),
            current_classification_proposals_canonicals=len(proposal_canonicals),
            effective_include_canonicals=sum(any(item.effective == "include" for item in effective_by_canonical[key]) for key in canonical_ids),
            effective_exclude_canonicals=sum(any(item.effective == "exclude" for item in effective_by_canonical[key]) for key in canonical_ids),
            effective_abstain_canonicals=sum(any(item.effective == "abstain" for item in effective_by_canonical[key]) for key in canonical_ids),
            review_required_canonicals=sum(any(item.needs_review for item in effective_by_canonical[key]) for key in canonical_ids),
            genre_routing_unresolved_canonicals=sum(bool(item.unresolved) for item in desired_states),
            canonicals_with_desired_membership=sum(bool(item.memberships) for item in desired_states),
            desired_memberships_total=sum(len(item.memberships) for item in desired_states),
            population_ready_canonicals=sum(item.bucket is ReadinessBucket.POPULATION_READY for item in values),
            local_classification_ready_canonicals=sum(item.bucket is ReadinessBucket.LOCAL_CLASSIFICATION_READY for item in values),
            research_needed_canonicals=sum(item.bucket is ReadinessBucket.RESEARCH_NEEDED for item in values),
            research_needed_canonical_ids=tuple(
                canonical_id for canonical_id in canonical_ids
                if buckets[canonical_id].bucket is ReadinessBucket.RESEARCH_NEEDED
            ),
            review_blocked_canonicals=sum(item.bucket is ReadinessBucket.REVIEW_BLOCKED for item in values),
            population_mapping_blocked_canonicals=sum(item.bucket is ReadinessBucket.POPULATION_MAPPING_BLOCKED for item in values),
            selected_action=selected_action,
            selections=selected,
            target_distribution=tuple(sorted(distribution.items())),
        )

    def worklist(self, limit: int = 25, *, unreviewed_only: bool = False) -> tuple[ClassificationWorklistItem, ...]:
        """Return bounded local human-review rows without consulting match candidates or providers."""

        if not 1 <= limit <= 100:
            raise ValueError("worklist limit must be between 1 and 100")
        canonical_rows = tuple(self.connection.execute(
            "SELECT canonical_track_id, display_title FROM canonical_tracks ORDER BY canonical_track_id"
        ))
        artists: dict[str, list[str]] = {}
        for row in self.connection.execute(
            "SELECT canonical_track_id, artist_name FROM canonical_track_artists "
            "ORDER BY canonical_track_id, position"
        ):
            artists.setdefault(row["canonical_track_id"], []).append(row["artist_name"])
        canonical_ids = tuple(row["canonical_track_id"] for row in canonical_rows)
        active_binding = self.connection.execute(
            "SELECT binding_id FROM platform_account_bindings WHERE active=1 ORDER BY bound_at"
        ).fetchone()
        current = {} if active_binding is None else current_account_canonical_identity_mappings(
            self.connection, active_binding["binding_id"], canonical_ids,
        )
        spotify = {} if active_binding is None else exact_spotify_population_mappings(
            self.connection, active_binding["binding_id"], canonical_ids,
        )
        reviews = self.review.effective_many(canonical_ids)
        review_ids = {value.canonical_track_id for value in reviews.values() if value.explicit is not None}
        routing = GenreRoutingService(self.connection, self.bundle)
        items: list[ClassificationWorklistItem] = []
        for row in canonical_rows:
            canonical_id = row["canonical_track_id"]
            reviewed = canonical_id in review_ids
            if unreviewed_only and reviewed:
                continue
            items.append(ClassificationWorklistItem(
                canonical_id, row["display_title"],
                tuple(artists.get(canonical_id, ())),
                self._worklist_mapping_state(
                    current.get(canonical_id, ()), spotify.get(canonical_id, ()), active_binding is None,
                ),
                reviewed, routing.show(canonical_id).state,
            ))
            if len(items) == limit:
                break
        return tuple(items)

    @staticmethod
    def _worklist_mapping_state(
        current: tuple[tuple[str, str, str | None], ...], spotify: tuple[tuple[str, str | None], ...],
        context_unavailable: bool,
    ) -> str:
        if context_unavailable:
            return "blocked_account_context"
        if not spotify and current:
            return "blocked_provider_mismatch"
        if not spotify:
            return "blocked_missing"
        if len(spotify) != 1:
            return "blocked_multiple"
        return "safe" if is_exact_spotify_population_mapping(spotify) else "blocked_invalid"

    @staticmethod
    def _mapping_counts(
        canonical_ids: tuple[str, ...],
        current_mappings: dict[str, tuple[tuple[str, str, str | None], ...]],
        spotify_mappings: dict[str, tuple[tuple[str, str | None], ...]],
        context_unavailable: bool,
    ) -> dict[str, int]:
        """Return exclusive primary reasons matching the Phase 11 mapping contract."""

        counts = {key: 0 for key in (
            "safe", "zero", "multiple", "context_unavailable", "provider_mismatch", "invalid",
            "any_current", "current_spotify",
        )}
        for canonical_id in canonical_ids:
            current = current_mappings.get(canonical_id, ())
            spotify = spotify_mappings.get(canonical_id, ())
            counts["any_current"] += int(bool(current))
            counts["current_spotify"] += int(bool(spotify))
            if context_unavailable:
                counts["context_unavailable"] += 1
            elif not spotify and current:
                counts["provider_mismatch"] += 1
            elif not spotify:
                counts["zero"] += 1
            elif len(spotify) != 1:
                counts["multiple"] += 1
            elif is_exact_spotify_population_mapping(spotify):
                counts["safe"] += 1
            else:
                counts["invalid"] += 1
        return counts

    @staticmethod
    def _bucket(
        canonical_id: str, desired: DesiredState, reviews: list[EffectiveReview],
        population_eligible: bool, qualifying_evidence: bool, derivable_evidence: bool,
        current_characteristics: bool,
        has_proposals: bool, research_actionable: bool, refresh_due: bool,
    ) -> ReadinessSelection:
        target_ids = tuple(sorted({item.output_target_id for item in desired.memberships}))
        if target_ids and population_eligible:
            return ReadinessSelection(canonical_id, ReadinessBucket.POPULATION_READY, "population", target_ids)
        if any(item.needs_review for item in reviews):
            return ReadinessSelection(canonical_id, ReadinessBucket.REVIEW_BLOCKED, "review")
        if not population_eligible:
            return ReadinessSelection(canonical_id, ReadinessBucket.POPULATION_MAPPING_BLOCKED, "resolve_population_mapping")
        if refresh_due:
            return ReadinessSelection(canonical_id, ReadinessBucket.RESEARCH_NEEDED, "research_then_derive_then_classify")
        if current_characteristics and not has_proposals:
            return ReadinessSelection(canonical_id, ReadinessBucket.LOCAL_CLASSIFICATION_READY, "classify")
        if derivable_evidence and not current_characteristics:
            return ReadinessSelection(canonical_id, ReadinessBucket.LOCAL_CLASSIFICATION_READY, "derive_then_classify")
        if not qualifying_evidence and research_actionable:
            return ReadinessSelection(canonical_id, ReadinessBucket.RESEARCH_NEEDED, "research_then_derive_then_classify")
        return ReadinessSelection(canonical_id, ReadinessBucket.NO_ACTIONABLE_CLASSIFICATION, "none")

    @staticmethod
    def _select(
        buckets: dict[str, ReadinessSelection], limit: int,
    ) -> tuple[str, tuple[ReadinessSelection, ...]]:
        for bucket, action in (
            (ReadinessBucket.POPULATION_READY, "population"),
            (ReadinessBucket.LOCAL_CLASSIFICATION_READY, "local-classification"),
            (ReadinessBucket.RESEARCH_NEEDED, "research"),
        ):
            values = tuple(
                buckets[key] for key in sorted(buckets)
                if buckets[key].bucket is bucket
            )[:limit]
            if values:
                return action, values
        return "none", ()
