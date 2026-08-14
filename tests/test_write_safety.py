import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.domain.models import (
    MutationContext,
    MutationKind,
    PlaylistRole,
)
from spotify_playlist_taxonomy.domain.policies import authorize_v1_mutation


def managed_add(**overrides: object) -> MutationContext:
    values: dict[str, object] = {
        "mutation": MutationKind.ADD_ITEMS,
        "playlist_role": PlaylistRole.MANAGED,
        "stable_playlist_id_registered": True,
        "live_owner_validated": True,
        "dry_run_approved": True,
        "change_set_approved": True,
        "target_exists": True,
    }
    values.update(overrides)
    return MutationContext(**values)  # type: ignore[arg-type]


class WriteSafetyInvariantTests(unittest.TestCase):
    def test_fully_guarded_managed_addition_is_allowed(self) -> None:
        self.assertTrue(authorize_v1_mutation(managed_add()).allowed)

    def test_source_and_inbox_are_never_writable(self) -> None:
        for role in (PlaylistRole.SOURCE, PlaylistRole.INBOX):
            with self.subTest(role=role):
                result = authorize_v1_mutation(managed_add(playlist_role=role))
                self.assertFalse(result.allowed)
                self.assertIn("target is not explicitly registered as managed", result.reasons)

    def test_unknown_target_is_denied(self) -> None:
        result = authorize_v1_mutation(
            managed_add(playlist_role=None, stable_playlist_id_registered=False)
        )
        self.assertFalse(result.allowed)
        self.assertGreaterEqual(len(result.reasons), 2)

    def test_name_cannot_substitute_for_stable_registration(self) -> None:
        result = authorize_v1_mutation(managed_add(stable_playlist_id_registered=False))
        self.assertFalse(result.allowed)
        self.assertIn("stable target ID is not registered", result.reasons)

    def test_owner_and_approval_are_each_required(self) -> None:
        cases = (
            {"live_owner_validated": False},
            {"dry_run_approved": False},
            {"change_set_approved": False},
            {"target_exists": False},
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(authorize_v1_mutation(managed_add(**case)).allowed)

    def test_destructive_and_non_additive_operations_are_prohibited(self) -> None:
        prohibited = set(MutationKind) - {
            MutationKind.CREATE_PLAYLIST,
            MutationKind.ADD_ITEMS,
        }
        for mutation in prohibited:
            with self.subTest(mutation=mutation):
                result = authorize_v1_mutation(managed_add(mutation=mutation))
                self.assertFalse(result.allowed)
                self.assertIn("mutation kind is prohibited in V1", result.reasons)

    def test_creation_requires_approval_and_a_nonexistent_target(self) -> None:
        context = MutationContext(
            mutation=MutationKind.CREATE_PLAYLIST,
            playlist_role=PlaylistRole.MANAGED,
            stable_playlist_id_registered=False,
            live_owner_validated=False,
            dry_run_approved=True,
            change_set_approved=True,
            target_exists=False,
        )
        self.assertTrue(authorize_v1_mutation(context).allowed)
        self.assertFalse(
            authorize_v1_mutation(
                MutationContext(
                    mutation=MutationKind.CREATE_PLAYLIST,
                    playlist_role=PlaylistRole.MANAGED,
                    stable_playlist_id_registered=False,
                    live_owner_validated=False,
                    dry_run_approved=True,
                    change_set_approved=True,
                    target_exists=True,
                )
            ).allowed
        )


if __name__ == "__main__":
    unittest.main()
