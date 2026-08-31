from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.catalogue_operator import CatalogueOperatorService


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
TARGET = "output.vibe.chill"
CANONICAL = "canonical.synthetic.operator"
BINDING = "binding.synthetic.operator"


class CatalogueOperatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        self.assertEqual(20, apply_migrations(self.connection))
        self.bundle = load_phase4_bundle(CONFIG)
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.service = CatalogueOperatorService(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _canonical(self) -> None:
        SQLiteStateStore(self.connection).save_canonical_track(CanonicalTrack(CANONICAL, "Synthetic", ("Operator",)))

    def _run(self, run_id: str, state: str, *, retry_after: int | None = None) -> None:
        self.connection.execute(
            "INSERT INTO catalogue_completion_runs VALUES (?, ?, 'digest.synthetic', 'plan.synthetic', ?, NULL, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (run_id, f'["{CANONICAL}"]', state, retry_after),
        )
        self.connection.commit()

    def _recovery_attempt(self, *, target: str = TARGET, canonical: str = CANONICAL) -> None:
        self._canonical()
        self.connection.execute(
            "INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic', 'user.synthetic', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)",
            (BINDING,),
        )
        self.connection.execute(
            "INSERT INTO managed_target_registrations VALUES (?, 'registration.synthetic.operator', 'spotify', ?, 'playlist.synthetic', 'active', 'phase10_create', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)",
            (target, BINDING),
        )
        self.connection.commit()
        SQLiteStateStore(self.connection).save_track_identity(
            PlatformTrackIdentity("identity.synthetic.operator", IdentityProvider.SPOTIFY, "Synthetic", ("Operator",), "track.synthetic")
        )
        self.connection.execute(
            "INSERT INTO managed_target_addition_attempts VALUES ('attempt.synthetic.operator', ?, ?, 'identity.synthetic.operator', 'registration.synthetic.operator', 'recovery_required', 'ADD:synthetic', 1, NULL, '2026-01-01T00:00:00Z', NULL)",
            (target, canonical),
        )
        self.connection.commit()

    def test_no_active_run_with_unreviewed_work_generates_worklist(self) -> None:
        self._canonical()
        status = self.service.status()
        self.assertEqual("generate_worklist", status.next_action)
        self.assertIsNone(status.run)
        self.assertEqual(1, status.unreviewed_classification_canonicals)

    def test_paused_run_is_resumable_with_exact_confirmation(self) -> None:
        self._canonical()
        self._run("run.synthetic.resume", "paused_quota", retry_after=60)
        status = self.service.status()
        self.assertEqual("resume_population", status.next_action)
        self.assertEqual("RESUME:run.synthetic.resume", status.next_confirmation)
        self.assertEqual(60, status.run.retry_after_seconds if status.run else None)

    def test_recovery_is_prioritized_and_uses_only_the_exact_persisted_pair(self) -> None:
        self._run("run.synthetic.resume", "paused_quota")
        self._recovery_attempt()
        status = self.service.status()
        self.assertEqual("recovery_verify", status.next_action)
        self.assertEqual("RECOVER:output.vibe.chill:canonical.synthetic.operator", status.next_confirmation)
        self.assertEqual(TARGET, status.recovery.output_target_id if status.recovery else None)
        self.assertEqual(CANONICAL, status.recovery.canonical_track_id if status.recovery else None)

    def test_verified_recovery_allows_the_persisted_run_to_resume(self) -> None:
        self._recovery_attempt()
        self._run("run.synthetic.recovery", "recovery_required")
        self.connection.execute("UPDATE managed_target_addition_attempts SET state='confirmed_recovered'")
        self.connection.commit()
        status = self.service.status()
        self.assertEqual("resume_population", status.next_action)
        self.assertEqual("RESUME:run.synthetic.recovery", status.next_confirmation)

    def test_multiple_recovery_identities_never_guess_a_command(self) -> None:
        self._recovery_attempt()
        self.connection.execute(
            "INSERT INTO managed_target_addition_attempts VALUES ('attempt.synthetic.operator.second', ?, ?, 'identity.synthetic.operator', 'registration.synthetic.operator', 'started', 'ADD:synthetic', 0, NULL, '2026-01-02T00:00:00Z', NULL)",
            (TARGET, CANONICAL),
        )
        self.connection.commit()
        status = self.service.status()
        self.assertEqual("blocked_manual", status.next_action)
        self.assertIsNone(status.recovery)
        self.assertIsNone(status.next_confirmation)
        self.assertEqual(2, status.recovery_identity_count)

    def test_completed_without_remaining_work_is_complete_and_selection_is_deterministic(self) -> None:
        self._run("run.synthetic.complete", "complete")
        self.assertEqual("complete", self.service.status().next_action)
        self._canonical()
        self._run("run.synthetic.started", "started")
        self._run("run.synthetic.paused", "paused_quota")
        first, second = self.service.status(), self.service.status()
        self.assertEqual("run.synthetic.paused", first.run.run_id if first.run else None)
        self.assertEqual(first, second)

    def test_managed_provisioning_recovery_is_blocked_manual(self) -> None:
        self.connection.execute(
            "INSERT INTO platform_account_bindings VALUES (?, 'spotify', 'account.synthetic', 'user.synthetic', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)",
            (BINDING,),
        )
        self.connection.execute(
            "INSERT INTO managed_target_registrations VALUES (?, 'registration.synthetic.operator', 'spotify', ?, NULL, 'recovery_required', 'phase10_create', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 0)",
            (TARGET, BINDING),
        )
        self.connection.commit()
        self.assertEqual("blocked_manual", self.service.status().next_action)

    def test_cli_status_is_local_only_and_compact(self) -> None:
        self._canonical()
        data_dir = Path(self.temp.name) / "runtime"
        (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db")
        self.connection.backup(target)
        target.close()
        with patch("spotify_playlist_taxonomy.cli.SpotifyReadOnlyAdapter", side_effect=AssertionError("provider access is forbidden")):
            result = CliRunner().invoke(app, ["catalogue", "operator", "status", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("next_action: generate_worklist", result.output)
        self.assertIn("unreviewed_classification_canonicals: 1", result.output)


if __name__ == "__main__":
    unittest.main()
