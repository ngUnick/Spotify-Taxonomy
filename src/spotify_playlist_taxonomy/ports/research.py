"""Contracts for progressive, cached, independent music research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from spotify_playlist_taxonomy.domain.models import ResearchEvidence


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    request_id: str
    canonical_track_id: str
    title: str
    artists: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchResult:
    request_id: str
    evidence: tuple[ResearchEvidence, ...]
    sufficient_for_classification: bool
    retryable: bool


class ResearchProvider(Protocol):
    provider_name: str

    def research(self, request: ResearchRequest) -> ResearchResult: ...
