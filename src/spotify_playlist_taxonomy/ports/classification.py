"""Provider-neutral batch classification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from spotify_playlist_taxonomy.domain.models import ClassificationProposal


@dataclass(frozen=True, slots=True)
class ClassificationBatchItem:
    canonical_track_id: str
    evidence_ids: tuple[str, ...]
    characteristic_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClassificationBatch:
    batch_id: str
    schema_version: str
    taxonomy_version: str
    specification_version: str
    items: tuple[ClassificationBatchItem, ...]


@dataclass(frozen=True, slots=True)
class ClassificationBatchResult:
    batch_id: str
    schema_version: str
    proposals: tuple[ClassificationProposal, ...]
    unresolved_track_ids: tuple[str, ...]


class BatchClassifier(Protocol):
    """Replaceable reasoning engine; intentionally has no platform write methods."""

    def classify_batch(self, batch: ClassificationBatch) -> ClassificationBatchResult: ...
