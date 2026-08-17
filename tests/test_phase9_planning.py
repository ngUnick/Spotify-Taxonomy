from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa:F401

from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, CanonicalTrack
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase9 import DryRunOutcome, ManagedTargetSnapshot, Phase9Service


ROOT = Path(__file__).resolve().parents[1]


class Phase9PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        self.bundle = load_phase4_bundle(ROOT / "config" / "application.yaml")
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.canonical_id = "canonical.synthetic.phase9"
        SQLiteStateStore(self.connection).save_canonical_track(CanonicalTrack(self.canonical_id, "Synthetic Phase Nine", ("Invented",)))
        self.service = Phase9Service(self.connection, self.bundle)

    def tearDown(self) -> None:
        self.connection.close(); self.temp.cleanup()

    def _include(self, semantic_id: str) -> None:
        spec = next(item for item in self.bundle.specifications if item.logical_playlist_id == semantic_id)
        self.connection.execute(
            "INSERT INTO phase6_proposals VALUES (?, ?, ?, 'include', 'supported', .9, ?, ?, ?, 'synthetic.v1', ?, ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')",
            (f"proposal.synthetic.{semantic_id}", self.canonical_id, semantic_id, spec.specification_version, self.bundle.taxonomy.taxonomy_version, self.bundle.vocabulary.vocabulary_version, self.bundle.digest, f"input.{semantic_id}"),
        )
        self.connection.commit()

    def _characteristic(self, name: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO phase6_characteristic_derivations VALUES (?, ?, ?, ?, 'enum', 'deterministic_rule', .9, 'synthetic.v1', ?, ?, ?, 'current', 'synthetic', '2026-01-01T00:00:00Z')",
            (f"derivation.{name}", self.canonical_id, name, json.dumps(value), self.bundle.vocabulary.vocabulary_version, self.bundle.digest, f"input.{name}"),
        )
        self.connection.commit()

    def test_vibe_and_activity_map_to_unsplit_targets_deterministically(self) -> None:
        self._include("vibe.chill"); self._include("activity.driving")
        first, second = self.service.desired(self.canonical_id), self.service.desired(self.canonical_id)
        self.assertEqual(first, second)
        self.assertEqual({"output.vibe.chill", "output.activity.driving"}, {item.output_target_id for item in first.memberships})

    def test_genre_routes_greek_foreign_and_unresolved_downstream_of_inclusion(self) -> None:
        self._include("genre.pop")
        self._characteristic("vocals_lyrics.lyrical_language_profile", "greek")
        self.assertEqual("output.genre.pop.greek", self.service.desired(self.canonical_id).memberships[0].output_target_id)
        self.connection.execute("UPDATE phase6_characteristic_derivations SET value_json=?", (json.dumps("non_greek"),)); self.connection.commit()
        self.assertEqual("output.genre.pop.foreign", self.service.desired(self.canonical_id).memberships[0].output_target_id)
        self.connection.execute("DELETE FROM phase6_characteristic_derivations"); self.connection.commit()
        state = self.service.desired(self.canonical_id)
        self.assertEqual((), state.memberships)
        self.assertEqual("genre.pop", state.unresolved[0].semantic_specification_id)

    def test_exclusion_precedence_and_generated_disagreement_do_not_become_desired(self) -> None:
        self._include("vibe.chill")
        review = Phase7Service(self.connection, self.bundle)
        review.decide(self.canonical_id, "vibe.chill", AssignmentDecision.EXCLUDE)
        self.assertEqual((), self.service.desired(self.canonical_id).memberships)
        review.clear(self.canonical_id, "vibe.chill")
        self.connection.execute("INSERT INTO codex_batches VALUES ('batch.synthetic', 'codex-batch.v1', ?, ?, 'imported', 'digest.synthetic', 1, 'synthetic', '2026-01-01T00:00:00Z')", (self.bundle.taxonomy.taxonomy_version, self.bundle.digest))
        self.connection.execute("INSERT INTO codex_batch_imports VALUES ('import.synthetic', 'batch.synthetic', 'codex-result.v1', 'result.synthetic', 1, 1, '2026-01-01T00:00:00Z')")
        self.connection.execute("INSERT INTO phase8_codex_proposals VALUES ('codex.synthetic', 'import.synthetic', 'batch.synthetic', 'item.001', ?, 'vibe.chill', 'exclude', ?, 'spec.vibe.chill.v1', ?, 'result.synthetic', 'synthetic', '2026-01-01T00:00:00Z')", (self.canonical_id, self.bundle.taxonomy.taxonomy_version, self.bundle.digest))
        self.connection.commit()
        self.assertEqual((), self.service.desired(self.canonical_id).memberships)

    def test_dry_run_is_addition_only_and_requires_authoritative_concrete_snapshot(self) -> None:
        self._include("vibe.chill")
        target = "output.vibe.chill"
        self.assertEqual(DryRunOutcome.UNPROVISIONED, self.service.plan((self.canonical_id,))[0].outcome)
        absent = ManagedTargetSnapshot(target, frozenset())
        self.assertEqual(DryRunOutcome.WOULD_ADD, self.service.plan((self.canonical_id,), (absent,))[0].outcome)
        present = ManagedTargetSnapshot(target, frozenset({self.canonical_id}))
        self.assertEqual(DryRunOutcome.NO_OP, self.service.plan((self.canonical_id,), (present,))[0].outcome)
        self.connection.execute("DELETE FROM phase6_proposals"); self.connection.commit()
        self.assertEqual(DryRunOutcome.PRESENT_NOT_DESIRED, self.service.plan((self.canonical_id,), (present,))[0].outcome)

    def test_source_and_inbox_registrations_never_supply_managed_current_state(self) -> None:
        self._include("vibe.chill")
        self.connection.execute("INSERT INTO platform_playlist_registrations(registration_id, provider, provider_playlist_id, role, logical_playlist_id, registered_at) VALUES ('source.synthetic', 'spotify', 'source.synthetic', 'source', 'vibe.chill', '2026-01-01T00:00:00Z')")
        self.connection.execute("INSERT INTO platform_playlist_registrations(registration_id, provider, provider_playlist_id, role, logical_playlist_id, registered_at) VALUES ('inbox.synthetic', 'spotify', 'inbox.synthetic', 'inbox', 'vibe.chill', '2026-01-01T00:00:00Z')")
        self.connection.commit()
        self.assertEqual(DryRunOutcome.UNPROVISIONED, self.service.plan((self.canonical_id,))[0].outcome)

    def test_phase9_has_fixed_configuration_boundary_and_no_write_surface(self) -> None:
        self.assertEqual(36, len(self.bundle.specifications))
        self.assertEqual(51, len(self.bundle.output_targets.targets))
        self.assertFalse(hasattr(self.service, "execute"))
        self.assertFalse(hasattr(self.service, "add_items"))


if __name__ == "__main__":
    unittest.main()
