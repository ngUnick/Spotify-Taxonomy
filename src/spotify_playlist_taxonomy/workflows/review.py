"""Local, account-scoped Phase 3 catalogue review operations."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from spotify_playlist_taxonomy.domain.ingestion import (
    CandidateReview,
    CatalogueStatus,
    IdentityReview,
    LinkReview,
    MatchCandidate,
    MatchCandidateState,
)
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store


class Phase3ReviewService:
    def __init__(
        self,
        *,
        store: Phase3Store,
        binding_id: str,
        id_factory: Callable[[str], str] = lambda prefix: f"{prefix}.{uuid.uuid4().hex}",
    ) -> None:
        self.store = store
        self.binding_id = binding_id
        self.id_factory = id_factory

    def status(self) -> CatalogueStatus:
        return self.store.catalogue_status(self.binding_id)

    def identity(self, identity_id: str) -> IdentityReview:
        return self.store.identity_review(self.binding_id, identity_id)

    def candidates(
        self, state: MatchCandidateState | None = None, limit: int | None = None
    ) -> tuple[MatchCandidate, ...]:
        return self.store.candidates(self.binding_id, state, limit)

    def candidate(self, candidate_id: str) -> CandidateReview:
        return self.store.candidate_review(self.binding_id, candidate_id)

    def links(self) -> tuple[LinkReview, ...]:
        return self.store.links(self.binding_id)

    def linked_identity_ids_for_canonical(
        self, canonical_track_id: str
    ) -> tuple[str, ...]:
        return self.store.linked_identity_ids_for_canonical(
            self.binding_id, canonical_track_id
        )

    def confirm_existing(self, candidate_id: str, canonical_id: str) -> None:
        self.store.confirm_candidate(
            self.binding_id,
            candidate_id,
            canonical_id=canonical_id,
            create_new=False,
            link_id=self.id_factory("link"),
            event_id=self.id_factory("link-event"),
        )

    def confirm_new(self, candidate_id: str) -> str:
        canonical_id = self.id_factory("canonical")
        self.store.confirm_candidate(
            self.binding_id,
            candidate_id,
            canonical_id=canonical_id,
            create_new=True,
            link_id=self.id_factory("link"),
            event_id=self.id_factory("link-event"),
        )
        return canonical_id

    def reject(self, candidate_id: str) -> None:
        self.store.reject_candidate(self.binding_id, candidate_id)

    def unlink(self, identity_id: str) -> None:
        self.store.unlink_identity(
            self.binding_id, identity_id, self.id_factory("link-event")
        )
