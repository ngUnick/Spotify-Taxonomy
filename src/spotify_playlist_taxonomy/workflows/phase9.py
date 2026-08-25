"""Deterministic, local-only desired-state and dry-run planning for Phase 9."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from spotify_playlist_taxonomy.config import Phase4Bundle, RoutingOutcome
from spotify_playlist_taxonomy.workflows.genre_routing import GenreRoutingService
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service


class DryRunOutcome(StrEnum):
    NO_OP = "no_op"
    WOULD_ADD = "would_add"
    UNPROVISIONED = "unprovisioned"
    UNRESOLVED = "unresolved"
    OUT_OF_SCOPE = "out_of_scope"
    PRESENT_NOT_DESIRED = "present_not_desired"


@dataclass(frozen=True, slots=True)
class DesiredMembership:
    canonical_track_id: str
    semantic_specification_id: str
    output_target_id: str
    taxonomy_version: str
    specification_version: str
    routing_policy_version: str | None
    routing_outcome: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class UnresolvedRouting:
    canonical_track_id: str
    semantic_specification_id: str
    taxonomy_version: str
    specification_version: str
    routing_policy_version: str
    reason: str


@dataclass(frozen=True, slots=True)
class DesiredState:
    canonical_track_id: str
    memberships: tuple[DesiredMembership, ...]
    unresolved: tuple[UnresolvedRouting, ...]


@dataclass(frozen=True, slots=True)
class ManagedTargetSnapshot:
    """An authoritative current membership snapshot supplied by a future managed-target reader."""

    output_target_id: str
    canonical_track_ids: frozenset[str]
    authoritative: bool = True


@dataclass(frozen=True, slots=True)
class DryRunPlanEntry:
    canonical_track_id: str
    output_target_id: str | None
    outcome: DryRunOutcome
    reason: str


class Phase9Service:
    """Read-only derived planning; it owns no platform reader or writer dependency."""

    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection = connection
        self.bundle = bundle
        self.review = Phase7Service(connection, bundle)
        self.genre_routing = GenreRoutingService(connection, bundle)
        self.specifications = {item.logical_playlist_id: item for item in bundle.specifications}
        self.targets = {
            (item.logical_playlist_id, None if item.variant_id is None else item.variant_id.value): item
            for item in bundle.output_targets.targets
        }

    def desired(self, canonical_track_id: str) -> DesiredState:
        return self._desired(canonical_track_id)

    def desired_many(self, canonical_track_ids: tuple[str, ...]) -> tuple[DesiredState, ...]:
        """Project many desired states without changing the Phase 9 routing semantics."""

        effective = self.review.effective_many(canonical_track_ids)
        routing_values = self._routing_values_many(canonical_track_ids)
        return tuple(
            self._desired(canonical_id, effective, routing_values.get(canonical_id, {}))
            for canonical_id in canonical_track_ids
        )

    def _desired(
        self, canonical_track_id: str,
        effective_many: dict[tuple[str, str], object] | None = None,
        routing_values: dict[str, str] | None = None,
    ) -> DesiredState:
        memberships: list[DesiredMembership] = []
        unresolved: list[UnresolvedRouting] = []
        for semantic_id in sorted(self.specifications):
            effective = (
                self.review.effective(canonical_track_id, semantic_id)
                if effective_many is None
                else effective_many.get((canonical_track_id, semantic_id))
            )
            if effective is None:
                continue
            if effective.effective != "include" or effective.needs_review or effective.stale:
                continue
            specification = self.specifications[semantic_id]
            if semantic_id.startswith("genre."):
                outcome, reason = self._route_genre(canonical_track_id, routing_values)
                if outcome is RoutingOutcome.UNRESOLVED:
                    unresolved.append(UnresolvedRouting(
                        canonical_track_id, semantic_id, self.bundle.taxonomy.taxonomy_version,
                        specification.specification_version,
                        self.bundle.genre_variant_routing.routing_policy_version, reason,
                    ))
                    continue
                target = self.targets[(semantic_id, outcome.value)]
                memberships.append(DesiredMembership(
                    canonical_track_id, semantic_id, target.output_target_id,
                    self.bundle.taxonomy.taxonomy_version, specification.specification_version,
                    self.bundle.genre_variant_routing.routing_policy_version, outcome.value, reason,
                ))
                continue
            target = self.targets[(semantic_id, None)]
            memberships.append(DesiredMembership(
                canonical_track_id, semantic_id, target.output_target_id,
                self.bundle.taxonomy.taxonomy_version, specification.specification_version,
                None, None, "effective semantic inclusion maps to its unsplit target",
            ))
        return DesiredState(canonical_track_id, tuple(memberships), tuple(unresolved))

    def plan(
        self,
        canonical_track_ids: tuple[str, ...],
        current_targets: tuple[ManagedTargetSnapshot, ...] = (),
    ) -> tuple[DryRunPlanEntry, ...]:
        if not canonical_track_ids or len(canonical_track_ids) > 25 or len(set(canonical_track_ids)) != len(canonical_track_ids):
            raise ValueError("plan requires 1-25 distinct explicit canonical IDs")
        supplied = current_targets or self._registered_target_snapshots()
        snapshots = {item.output_target_id: item for item in supplied}
        entries: list[DryRunPlanEntry] = []
        for canonical_id in canonical_track_ids:
            desired = self.desired(canonical_id)
            desired_targets = {item.output_target_id for item in desired.memberships}
            for item in desired.memberships:
                snapshot = snapshots.get(item.output_target_id)
                if snapshot is None:
                    entries.append(DryRunPlanEntry(canonical_id, item.output_target_id, DryRunOutcome.UNPROVISIONED, "no authoritative managed-target mapping exists"))
                elif not snapshot.authoritative:
                    entries.append(DryRunPlanEntry(canonical_id, item.output_target_id, DryRunOutcome.OUT_OF_SCOPE, "managed target snapshot is not authoritative"))
                elif canonical_id in snapshot.canonical_track_ids:
                    entries.append(DryRunPlanEntry(canonical_id, item.output_target_id, DryRunOutcome.NO_OP, "desired membership is already present"))
                else:
                    entries.append(DryRunPlanEntry(canonical_id, item.output_target_id, DryRunOutcome.WOULD_ADD, "desired membership is absent from the authoritative managed target"))
            for item in desired.unresolved:
                entries.append(DryRunPlanEntry(canonical_id, None, DryRunOutcome.UNRESOLVED, item.reason))
            for target_id, snapshot in sorted(snapshots.items()):
                if snapshot.authoritative and canonical_id in snapshot.canonical_track_ids and target_id not in desired_targets:
                    entries.append(DryRunPlanEntry(canonical_id, target_id, DryRunOutcome.PRESENT_NOT_DESIRED, "present on a managed target but no removal is planned"))
        return tuple(entries)

    def _registered_target_snapshots(self) -> tuple[ManagedTargetSnapshot, ...]:
        """Recognize durable Phase 10 mappings without pretending membership was read."""
        try:
            rows = self.connection.execute(
                "SELECT output_target_id FROM managed_target_registrations WHERE state='active' AND policy_confirmed=1 "
                "ORDER BY output_target_id"
            )
        except sqlite3.OperationalError:
            return ()
        return tuple(
            ManagedTargetSnapshot(row["output_target_id"], frozenset(), authoritative=False)
            for row in rows
        )

    def _route_genre(
        self, canonical_track_id: str, values: dict[str, str] | None = None,
    ) -> tuple[RoutingOutcome, str]:
        explicit = self.genre_routing.show(canonical_track_id)
        if explicit.state == "current" and explicit.route is not None:
            return explicit.route, f"explicit human genre routing: {explicit.route.value}"
        values = self._routing_values(canonical_track_id) if values is None else values
        refs = self.bundle.genre_variant_routing.characteristic_references
        lexical = values.get(refs.lyrical_language_profile)
        if lexical is not None:
            outcome = self.bundle.genre_variant_routing.lexical_routes.get(lexical, RoutingOutcome.UNRESOLVED)
            return outcome, f"lyrical-language routing: {lexical}"
        vocal = values.get(refs.vocal_presence)
        if vocal in self.bundle.genre_variant_routing.instrumental_nonlexical.vocal_presence_values:
            regional = values.get(refs.regional_musical_association)
            if regional is not None:
                outcome = self.bundle.genre_variant_routing.instrumental_nonlexical.regional_routes.get(regional, RoutingOutcome.UNRESOLVED)
                if outcome is not RoutingOutcome.CONTINUE:
                    return outcome, f"instrumental regional routing: {regional}"
            project = values.get(refs.primary_project_scene_association)
            if project is not None:
                outcome = self.bundle.genre_variant_routing.instrumental_nonlexical.project_scene_routes.get(project, RoutingOutcome.UNRESOLVED)
                return outcome, f"instrumental project-scene routing: {project}"
        return RoutingOutcome.UNRESOLVED, "routing characteristics are absent, ambiguous, or insufficient"

    def _routing_values(self, canonical_track_id: str) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT characteristic_name, value_json FROM phase6_characteristic_derivations "
            "WHERE canonical_track_id=? AND state='current'",
            (canonical_track_id,),
        )
        values: dict[str, str] = {}
        for row in rows:
            try:
                value = json.loads(row["value_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(value, str):
                values[row["characteristic_name"]] = value
            elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
                values[row["characteristic_name"]] = value[0]
        return values

    def _routing_values_many(self, canonical_track_ids: tuple[str, ...]) -> dict[str, dict[str, str]]:
        wanted = set(canonical_track_ids)
        result = {canonical_id: {} for canonical_id in canonical_track_ids}
        if not wanted:
            return result
        references = self.bundle.genre_variant_routing.characteristic_references
        names = (
            references.lyrical_language_profile,
            references.vocal_presence,
            references.regional_musical_association,
            references.primary_project_scene_association,
        )
        for start in range(0, len(canonical_track_ids), 500):
            batch = canonical_track_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            name_placeholders = ",".join("?" for _ in names)
            rows = self.connection.execute(
                f"SELECT canonical_track_id, characteristic_name, value_json FROM phase6_characteristic_derivations "
                f"WHERE state='current' AND canonical_track_id IN ({placeholders}) "
                f"AND characteristic_name IN ({name_placeholders})",
                (*batch, *names),
            )
            for row in rows:
                try:
                    value = json.loads(row["value_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(value, str):
                    result[row["canonical_track_id"]][row["characteristic_name"]] = value
                elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
                    result[row["canonical_track_id"]][row["characteristic_name"]] = value[0]
        return result
