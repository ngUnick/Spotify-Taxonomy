"""Offline, deterministic Phase 6 derivation and semantic proposal workflow."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from spotify_playlist_taxonomy.config import ConditionOperator, Phase4Bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, CharacteristicOrigin, DerivedCharacteristic

DERIVATION_VERSION = "derivation-rules.v1"
EVALUATOR_VERSION = "semantic-evaluator.v1"


@dataclass(frozen=True, slots=True)
class ProposalView:
    logical_playlist_id: str
    decision: str
    state: str
    matched_strong: tuple[str, ...]
    matched_supporting: tuple[str, ...]
    matched_negative: tuple[str, ...]
    matched_hard_exclusions: tuple[str, ...]
    characteristic_names: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Phase6Service:
    """Reads persisted evidence only; it owns no provider or Spotify dependency."""

    _RULES = {
        "style_tradition": ("style.traditions", {"recording"}),
        "version_information": ("version.kind", {"recording"}),
        "work_context": ("work_context.function", {"recording", "work"}),
        "lyrical_language": ("vocals_lyrics.lyrical_language_profile", {"recording", "work"}),
        "cultural_regional_context": ("cultural_context.regional_musical_association", {"recording", "work", "project"}),
        "project_scene_context": ("cultural_context.primary_project_scene_association", {"project", "work"}),
    }

    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.definitions = {item.characteristic_id: item for item in bundle.vocabulary.characteristics}

    def derive(self, canonical_track_id: str) -> tuple[DerivedCharacteristic, ...]:
        rows = self.qualifying_evidence(canonical_track_id)
        candidates: dict[str, list[tuple[sqlite3.Row, Any]]] = {}
        for row in rows:
            if row["subject_match"] != "exact" or row["quality_band"] not in {"high", "medium"}:
                continue
            rule = self._RULES.get(row["evidence_type"] or row["claim_key"])
            if rule is None or row["subject_scope"] not in rule[1]:
                continue
            try:
                raw = json.loads(row["normalized_value"])
            except (TypeError, ValueError):
                continue
            value = raw.get("value") if isinstance(raw, dict) else None
            if value is not None:
                candidates.setdefault(rule[0], []).append((row, value))
        result: list[DerivedCharacteristic] = []
        for name, asserted in candidates.items():
            item = self._derive_one(canonical_track_id, name, asserted)
            if item is not None:
                result.append(item)
        return tuple(result)

    def qualifying_evidence(self, canonical_track_id: str | None = None) -> tuple[sqlite3.Row, ...]:
        """Return exactly the persisted evidence Phase 6 may derive from, without writes."""

        query = "SELECT * FROM research_evidence WHERE evidence_state='active'"
        params: tuple[object, ...] = ()
        if canonical_track_id is not None:
            query += " AND canonical_track_id=?"
            params = (canonical_track_id,)
        rows = self.connection.execute(query + " ORDER BY canonical_track_id, evidence_id", params)
        return tuple(
            row for row in rows
            if row["subject_match"] == "exact"
            and row["quality_band"] in {"high", "medium"}
            and (row["evidence_type"] or row["claim_key"]) in self._RULES
            and row["subject_scope"] in self._RULES[row["evidence_type"] or row["claim_key"]][1]
        )

    def _derive_one(self, canonical_id: str, name: str, asserted: list[tuple[sqlite3.Row, Any]]) -> DerivedCharacteristic | None:
        definition = self.definitions.get(name)
        if definition is None:
            return None
        schema = definition.value_schema
        allowed = {value.value for value in schema.allowed_values}
        aliases = {key.casefold(): value for key, value in definition.normalization.aliases.items()}
        normalized: list[Any] = []
        evidence_ids: list[str] = []
        for row, raw in asserted:
            values = raw if isinstance(raw, list) else [raw]
            converted = []
            for value in values:
                if isinstance(value, str):
                    value = aliases.get(value.casefold(), value)
                if schema.type.value == "boolean":
                    if isinstance(value, bool): converted.append(value)
                elif value in allowed:
                    converted.append(value)
            if converted:
                normalized.extend(converted); evidence_ids.append(row["evidence_id"])
        if not normalized:
            return None
        if schema.type.value == "multi_enum":
            value: Any = sorted(set(normalized))
            state = "current"
        else:
            unique = set(normalized)
            value = next(iter(unique)) if len(unique) == 1 else sorted(str(v) for v in unique)
            state = "current" if len(unique) == 1 else "conflicting"
        input_digest = _digest({"evidence": sorted(evidence_ids), "value": value, "rule": DERIVATION_VERSION})
        derivation_id = f"derivation.{_digest([canonical_id, name, input_digest])[:24]}"
        return DerivedCharacteristic(derivation_id, canonical_id, name, json.dumps(value, separators=(",", ":")), CharacteristicOrigin.DETERMINISTIC_RULE,
            tuple(sorted(evidence_ids)), 0.9, DERIVATION_VERSION, f"Eligible exact evidence derived {name}.",
            schema.type.value, self.bundle.vocabulary.vocabulary_version, self.bundle.digest, state, input_digest)

    def persist_derivations(self, items: tuple[DerivedCharacteristic, ...]) -> tuple[DerivedCharacteristic, ...]:
        for item in items:
            exists = self.connection.execute("SELECT derivation_id FROM phase6_characteristic_derivations WHERE derivation_id=?", (item.characteristic_id,)).fetchone()
            if exists is not None: continue
            self.connection.execute("UPDATE phase6_characteristic_derivations SET state='superseded' WHERE canonical_track_id=? AND characteristic_name=? AND state='current'", (item.canonical_track_id, item.name))
            self.connection.execute("INSERT INTO phase6_characteristic_derivations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.characteristic_id, item.canonical_track_id, item.name, item.value, item.value_type, item.origin.value, item.confidence, item.derivation_version, item.vocabulary_version, item.bundle_digest, item.input_digest, item.state, item.justification_summary, _now()))
            self.connection.executemany("INSERT INTO phase6_characteristic_evidence VALUES (?, ?)", [(item.characteristic_id, x) for x in item.evidence_ids])
        self.connection.commit()
        return items

    def characteristics(self, canonical_track_id: str) -> tuple[DerivedCharacteristic, ...]:
        rows = self.connection.execute("SELECT * FROM phase6_characteristic_derivations WHERE canonical_track_id=? AND state IN ('current','conflicting') ORDER BY characteristic_name", (canonical_track_id,)).fetchall()
        answer=[]
        for row in rows:
            evidence=tuple(x[0] for x in self.connection.execute("SELECT evidence_id FROM phase6_characteristic_evidence WHERE derivation_id=? ORDER BY evidence_id", (row["derivation_id"],)))
            answer.append(DerivedCharacteristic(row["derivation_id"],row["canonical_track_id"],row["characteristic_name"],row["value_json"],CharacteristicOrigin(row["origin"]),evidence,row["confidence"],row["derivation_version"],row["justification_summary"],row["value_type"],row["vocabulary_version"],row["bundle_digest"],row["state"],row["input_digest"]))
        return tuple(answer)

    def classify(self, canonical_track_id: str) -> tuple[ProposalView, ...]:
        characteristics = self.characteristics(canonical_track_id)
        values = {x.name: json.loads(x.value) for x in characteristics if x.state == "current"}
        conflicts = {x.name for x in characteristics if x.state == "conflicting"}
        evidence = {x.name: x.evidence_ids for x in characteristics}
        result=[]
        for spec in self.bundle.specifications:
            strong=self._matched(spec.strong_indicators, values); supporting=self._matched(spec.supporting_indicators, values)
            negative=self._matched(spec.negative_indicators, values); hard=self._matched(spec.hard_exclusions, values)
            referenced={c.characteristic_id for group in (spec.strong_indicators,spec.supporting_indicators,spec.negative_indicators,spec.hard_exclusions) for i in group for c in i.conditions}
            if hard: decision,state=AssignmentDecision.EXCLUDE.value,"hard_excluded"
            elif conflicts & referenced: decision,state=AssignmentDecision.ABSTAIN.value,"conflicting"
            elif strong and not negative: decision,state=AssignmentDecision.INCLUDE.value,"supported"
            else: decision,state=AssignmentDecision.ABSTAIN.value,"insufficient"
            used=tuple(sorted({c for c in referenced if c in values or c in conflicts}))
            evidence_ids=tuple(sorted({e for c in used for e in evidence.get(c,())}))
            view=ProposalView(spec.logical_playlist_id,decision,state,tuple(strong),tuple(supporting),tuple(negative),tuple(hard),used,evidence_ids)
            self._persist_proposal(canonical_track_id,spec,view,characteristics)
            result.append(view)
        self.connection.commit(); return tuple(result)

    def _matched(self, indicators: list[Any], values: dict[str, Any]) -> list[str]:
        return [item.indicator_id for item in indicators if self._indicator(item, values)]

    def _indicator(self, indicator: Any, values: dict[str, Any]) -> bool:
        checks=[]
        for condition in indicator.conditions:
            if condition.characteristic_id not in values: checks.append(False); continue
            value=values[condition.characteristic_id]; wanted=condition.values; op=condition.operator
            if op is ConditionOperator.IS: checks.append(value == wanted[0])
            elif op is ConditionOperator.EQUALS: checks.append(value == wanted[0])
            elif op is ConditionOperator.ONE_OF: checks.append(value in wanted)
            elif op is ConditionOperator.NOT_ONE_OF: checks.append(value not in wanted)
            elif op is ConditionOperator.CONTAINS_ANY: checks.append(bool(set(value) & set(wanted)))
            elif op is ConditionOperator.CONTAINS_ALL: checks.append(set(wanted) <= set(value))
            elif op is ConditionOperator.CONTAINS_NONE: checks.append(not bool(set(value) & set(wanted)))
            else:
                definition = self.definitions[condition.characteristic_id]
                ranks = {item.value: item.rank for item in definition.value_schema.allowed_values}
                actual = ranks.get(value, value)
                expected = [ranks.get(item, item) for item in wanted]
                if not isinstance(actual, (int, float)) or not all(isinstance(item, (int, float)) for item in expected):
                    checks.append(False)
                elif op is ConditionOperator.AT_LEAST: checks.append(actual >= expected[0])
                elif op is ConditionOperator.AT_MOST: checks.append(actual <= expected[0])
                elif op is ConditionOperator.BETWEEN: checks.append(expected[0] <= actual <= expected[1])
                else: checks.append(False)
        return all(checks) if indicator.match == "all" else any(checks)

    def _persist_proposal(self, canonical_id: str, spec: Any, view: ProposalView, items: tuple[DerivedCharacteristic, ...]) -> None:
        digest=_digest({"bundle":self.bundle.digest,"spec":spec.specification_version,"characteristics":[x.characteristic_id for x in items]})
        proposal_id=f"proposal.{_digest([canonical_id,spec.logical_playlist_id,digest])[:24]}"
        if self.connection.execute("SELECT 1 FROM phase6_proposals WHERE proposal_id=?",(proposal_id,)).fetchone(): return
        explanation={"strong":view.matched_strong,"supporting":view.matched_supporting,"negative":view.matched_negative,"hard_exclusions":view.matched_hard_exclusions,"evidence_ids":view.evidence_ids}
        self.connection.execute("INSERT INTO phase6_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (proposal_id,canonical_id,spec.logical_playlist_id,view.decision,view.state,0.9 if view.decision=='include' else 0.0,spec.specification_version,self.bundle.taxonomy.taxonomy_version,self.bundle.vocabulary.vocabulary_version,EVALUATOR_VERSION,self.bundle.digest,digest,'needs_review',json.dumps(explanation,separators=(',',':')),_now()))
        by_name={x.name:x for x in items}
        self.connection.executemany("INSERT INTO phase6_proposal_derivations VALUES (?, ?)", [(proposal_id,by_name[name].characteristic_id) for name in view.characteristic_names if name in by_name])

    def proposals(self, canonical_track_id: str) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute("SELECT * FROM phase6_proposals WHERE canonical_track_id=? ORDER BY logical_playlist_id", (canonical_track_id,)))
