from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

import _bootstrap  # noqa:F401

from spotify_playlist_taxonomy.adapters.spotify.provisioning import (
    MANAGED_DESCRIPTION, PROVISIONING_SCOPE, SpotifyManagedPlaylistPolicyMismatch,
    SpotifyManagedPlaylistProvisioner,
)
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack
from spotify_playlist_taxonomy.ports.music_platform import PlatformAccountIdentity, PlatformPlaylistSummary
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase10 import Phase10Service, ProvisioningError, RecoveryRequiredError
from spotify_playlist_taxonomy.workflows.phase9 import DryRunOutcome, Phase9Service


ROOT = Path(__file__).resolve().parents[1]


class _Creator:
    def __init__(self, result: str = "syntheticplaylist01", fail: bool = False) -> None:
        self.result, self.fail, self.calls = result, fail, []

    def create_private_managed_playlist(self, account_id: str, name: str) -> str:
        self.calls.append((account_id, name))
        if self.fail:
            raise RuntimeError("synthetic uncertain transport failure")
        return self.result


class _FailureCreator:
    def __init__(self, kind: SpotifyErrorKind) -> None:
        self.kind, self.calls = kind, 0

    def create_private_managed_playlist(self, account_id: str, name: str) -> str:
        self.calls += 1
        raise SpotifyAdapterError(self.kind, f"{self.kind.value}: synthetic")


class _Verifier:
    def __init__(self, playlist: PlatformPlaylistSummary, account: PlatformAccountIdentity | None = None) -> None:
        self.playlist = playlist
        self.account = account or PlatformAccountIdentity("account.synthetic.phase10", "user.synthetic")
        self.playlist_reads: list[str] = []

    def read_current_account(self) -> PlatformAccountIdentity:
        return self.account

    def read_playlist(self, playlist_id: str) -> PlatformPlaylistSummary:
        self.playlist_reads.append(playlist_id)
        return self.playlist


def _playlist(*, playlist_id: str = "syntheticplaylist01", owner: str = "user.synthetic", public: bool | None = False, collaborative: bool = False, name: str = "Chill", description: str | None = "Managed by Spotify Taxonomy.") -> PlatformPlaylistSummary:
    return PlatformPlaylistSummary(playlist_id, name, owner, collaborative, public, 0, "synthetic", description)


class Phase10ProvisioningTests(unittest.TestCase):
    target = "output.vibe.chill"
    binding = "binding.synthetic.phase10"
    account = "account.synthetic.phase10"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        self.bundle = load_phase4_bundle(ROOT / "config" / "application.yaml")
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.connection.execute(
            "INSERT INTO platform_account_bindings VALUES (?, 'spotify', ?, 'user.synthetic', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)",
            (self.binding, self.account),
        )
        self.connection.commit()
        self.service = Phase10Service(self.connection, self.bundle)

    def tearDown(self) -> None:
        self.connection.close(); self.temp.cleanup()

    def test_preview_is_local_and_apply_creates_one_private_registered_target_idempotently(self) -> None:
        preview = self.service.preview(self.target, account_binding_id=self.binding, scopes=(PROVISIONING_SCOPE,))
        self.assertEqual("preview", preview.state)
        creator = _Creator()
        result = self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
        self.assertEqual("active", result.state)
        self.assertEqual("syntheticplaylist01", result.provider_playlist_id)
        self.assertIn("provider-confirmed non-collaborative", result.reason)
        self.assertEqual([(self.account, "Chill")], creator.calls)
        again = self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
        self.assertEqual(result, again)
        self.assertEqual(1, len(creator.calls))

    def test_legacy_active_registration_is_not_described_as_provider_policy_confirmed(self) -> None:
        self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        self.connection.execute("UPDATE managed_target_registrations SET policy_confirmed=0 WHERE output_target_id=?", (self.target,))
        self.connection.commit()
        self.assertIn("not confirmed", self.service.status(self.target).reason)

    def test_exact_id_verification_confirms_policy_and_reports_metadata_drift_without_ownership_transfer(self) -> None:
        self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        self.connection.execute("UPDATE managed_target_registrations SET policy_confirmed=0 WHERE output_target_id=?", (self.target,))
        self.connection.commit()
        verifier = _Verifier(_playlist(name="Different visible name", description="Different description"))
        result = self.service.verify_policy(self.target, verifier)
        self.assertTrue(result.policy_confirmed)
        self.assertFalse(result.name_matches)
        self.assertFalse(result.description_matches)
        self.assertEqual(["syntheticplaylist01"], verifier.playlist_reads)
        self.assertIn("provider-confirmed", self.service.status(self.target).reason)
        self.assertFalse(hasattr(verifier, "search_playlists"))
        self.assertFalse(hasattr(verifier, "write_playlist"))

    def test_verification_fails_closed_for_identity_owner_and_reports_bounded_policy_mismatch(self) -> None:
        cases = (
            _Verifier(_playlist(playlist_id="differentplaylist")),
            _Verifier(_playlist(owner="different-user")),
            _Verifier(_playlist(public=True)),
            _Verifier(_playlist(collaborative=True)),
            _Verifier(_playlist(), PlatformAccountIdentity("different-account", "user.synthetic")),
        )
        for verifier in cases:
            with self.subTest(verifier=verifier.playlist.playlist_id):
                self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
                self.connection.execute("UPDATE managed_target_registrations SET policy_confirmed=0 WHERE output_target_id=?", (self.target,)); self.connection.commit()
                if verifier.playlist.playlist_id == "syntheticplaylist01" and verifier.playlist.owner_user_id == "user.synthetic" and verifier.account.account_id == self.account:
                    result = self.service.verify_policy(self.target, verifier)
                    self.assertEqual(verifier.playlist.collaborative is False, result.policy_confirmed)
                    self.assertEqual(str(verifier.playlist.public).lower(), result.public_state)
                    self.assertEqual(str(verifier.playlist.collaborative).lower(), result.collaborative_state)
                else:
                    with self.assertRaises(ProvisioningError):
                        self.service.verify_policy(self.target, verifier)
                if verifier.playlist.collaborative is False and verifier.playlist.playlist_id == "syntheticplaylist01" and verifier.playlist.owner_user_id == "user.synthetic" and verifier.account.account_id == self.account:
                    self.assertIn("provider-confirmed", self.service.status(self.target).reason)
                else:
                    self.assertIn("not confirmed", self.service.status(self.target).reason)
                self.connection.execute("DELETE FROM managed_target_registrations WHERE output_target_id=?", (self.target,)); self.connection.commit()

    def test_scope_confirmation_and_binding_mismatch_fail_closed(self) -> None:
        self.assertEqual("blocked", self.service.preview(self.target, account_binding_id=self.binding, scopes=()).state)
        with self.assertRaises(ProvisioningError):
            self.service.apply(self.target, "wrong", account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        with self.assertRaises(ProvisioningError):
            self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id="different", scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        with self.assertRaises(ProvisioningError):
            self.service.status("source.synthetic")

    def test_uncertain_creation_is_recovery_required_and_never_retried(self) -> None:
        creator = _Creator(fail=True)
        with self.assertRaises(RecoveryRequiredError):
            self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
        self.assertEqual("recovery_required", self.service.status(self.target).state)
        with self.assertRaises(ProvisioningError):
            self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
        self.assertEqual(1, len(creator.calls))

    def test_definite_local_or_provider_rejection_leaves_no_registration_and_allows_retry(self) -> None:
        for kind in (SpotifyErrorKind.REQUEST_NOT_SENT, SpotifyErrorKind.PROVIDER_REJECTED):
            with self.subTest(kind=kind):
                creator = _FailureCreator(kind)
                with self.assertRaisesRegex(ProvisioningError, kind.value):
                    self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
                self.assertEqual("unprovisioned", self.service.status(self.target).state)
        self.assertEqual("active", self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator()).state)

    def test_confirmed_creation_with_local_registration_failure_retains_identity_for_recovery(self) -> None:
        self.connection.execute("CREATE TRIGGER reject_active_registration BEFORE UPDATE OF state ON managed_target_registrations WHEN NEW.state='active' BEGIN SELECT RAISE(FAIL, 'synthetic'); END")
        self.connection.commit()
        with self.assertRaisesRegex(RecoveryRequiredError, "local_registration_failed_after_remote_success"):
            self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        status = self.service.status(self.target)
        self.assertEqual("recovery_required", status.state)
        self.assertEqual("syntheticplaylist01", status.provider_playlist_id)

    def test_provider_policy_mismatch_never_activates_and_retains_confirmed_identity(self) -> None:
        class MismatchCreator:
            def create_private_managed_playlist(self, account_id: str, name: str) -> str:
                raise SpotifyManagedPlaylistPolicyMismatch("syntheticplaylist01")
        with self.assertRaisesRegex(RecoveryRequiredError, "provider_policy_mismatch"):
            self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=MismatchCreator())
        status = self.service.status(self.target)
        self.assertEqual("recovery_required", status.state)
        self.assertEqual("syntheticplaylist01", status.provider_playlist_id)

    def test_explicit_abandon_unknown_preserves_audit_and_makes_later_manual_provisioning_possible(self) -> None:
        creator = _Creator(fail=True)
        with self.assertRaises(RecoveryRequiredError):
            self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
        before = self.connection.execute("SELECT COUNT(*) AS count FROM managed_target_abandoned_unknown_attempts").fetchone()["count"]
        with self.assertRaises(ProvisioningError):
            self.service.abandon_unknown(self.target, "ABANDON-UNKNOWN:wrong.target")
        self.assertEqual("recovery_required", self.service.status(self.target).state)
        self.assertEqual(before, self.connection.execute("SELECT COUNT(*) AS count FROM managed_target_abandoned_unknown_attempts").fetchone()["count"])
        status = self.service.abandon_unknown(self.target, f"ABANDON-UNKNOWN:{self.target}")
        self.assertEqual("unprovisioned", status.state)
        self.assertIn("explicitly abandoned", status.reason)
        history = self.connection.execute("SELECT * FROM managed_target_abandoned_unknown_attempts").fetchone()
        self.assertEqual("abandoned_unknown", history["outcome_state"])
        self.assertEqual("recovery_required", history["original_state"])
        self.assertIsNone(history["provider_playlist_id"])
        self.assertEqual("preview", self.service.preview(self.target, account_binding_id=self.binding, scopes=(PROVISIONING_SCOPE,)).state)
        result = self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        self.assertEqual("active", result.state)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) AS count FROM managed_target_abandoned_unknown_attempts").fetchone()["count"])

    def test_abandon_unknown_rejects_active_and_known_identity_recovery_states(self) -> None:
        self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=_Creator())
        with self.assertRaises(ProvisioningError):
            self.service.abandon_unknown(self.target, f"ABANDON-UNKNOWN:{self.target}")
        self.connection.execute("UPDATE managed_target_registrations SET state='recovery_required' WHERE output_target_id=?", (self.target,))
        self.connection.commit()
        with self.assertRaises(ProvisioningError):
            self.service.abandon_unknown(self.target, f"ABANDON-UNKNOWN:{self.target}")

    def test_phase9_recognizes_registration_but_does_not_invent_current_membership(self) -> None:
        creator = _Creator()
        self.service.apply(self.target, self.target, account_binding_id=self.binding, account_id=self.account, scopes=(PROVISIONING_SCOPE,), creator=creator)
        SQLiteStateStore(self.connection).save_canonical_track(CanonicalTrack("canonical.synthetic.phase10", "Synthetic Phase Ten", ("Invented",)))
        specification = next(item for item in self.bundle.specifications if item.logical_playlist_id == "vibe.chill")
        self.connection.execute(
            "INSERT INTO phase6_proposals VALUES ('proposal.phase10', 'canonical.synthetic.phase10', 'vibe.chill', 'include', 'supported', .9, ?, ?, ?, 'synthetic.v1', 'input', ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')",
            (specification.specification_version, self.bundle.taxonomy.taxonomy_version, self.bundle.vocabulary.vocabulary_version, self.bundle.digest),
        )
        self.connection.commit()
        # No provider membership read happened, so Phase 9 correctly remains out of scope.
        phase9 = Phase9Service(self.connection, self.bundle)
        plan = phase9.plan(("canonical.synthetic.phase10",))
        self.assertEqual(DryRunOutcome.OUT_OF_SCOPE, plan[0].outcome)
        self.connection.execute("UPDATE managed_target_registrations SET policy_confirmed=0 WHERE output_target_id=?", (self.target,)); self.connection.commit()
        self.assertEqual(DryRunOutcome.UNPROVISIONED, phase9.plan(("canonical.synthetic.phase10",))[0].outcome)
        self.service.verify_policy(self.target, _Verifier(_playlist()))
        self.assertEqual(DryRunOutcome.OUT_OF_SCOPE, phase9.plan(("canonical.synthetic.phase10",))[0].outcome)


class ProvisioningAdapterTests(unittest.TestCase):
    def test_create_payload_is_private_exact_and_has_no_generic_mutation_surface(self) -> None:
        seen: dict[str, object] = {}
        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"], seen["json"] = str(request.url), request.content
            return httpx.Response(201, json={"id": "syntheticplaylist01", "public": False, "collaborative": False})
        class Token:
            def access_token(self, *, force_refresh: bool = False) -> str: return "synthetic"
        adapter = SpotifyManagedPlaylistProvisioner(token_provider=Token(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual("syntheticplaylist01", adapter.create_private_managed_playlist("syntheticuser", "Synthetic target"))
        self.assertIn("/v1/me/playlists", str(seen["url"]))
        self.assertEqual(b'{"name":"Synthetic target","description":"' + MANAGED_DESCRIPTION.encode() + b'","public":false,"collaborative":false}', seen["json"])
        self.assertFalse(hasattr(adapter, "add_items"))
        self.assertFalse(hasattr(adapter, "remove_items"))

    def test_create_public_value_is_informational_but_collaboration_mismatch_is_rejected(self) -> None:
        class Token:
            def access_token(self, *, force_refresh: bool = False) -> str: return "synthetic"
        def public_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"id": "syntheticplaylist01", "public": True, "collaborative": False})
        adapter = SpotifyManagedPlaylistProvisioner(token_provider=Token(), http_client=httpx.Client(transport=httpx.MockTransport(public_response)))
        self.assertEqual("syntheticplaylist01", adapter.create_private_managed_playlist("syntheticuser", "Synthetic"))
        def collaborative_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"id": "syntheticplaylist02", "public": False, "collaborative": True})
        adapter = SpotifyManagedPlaylistProvisioner(token_provider=Token(), http_client=httpx.Client(transport=httpx.MockTransport(collaborative_response)))
        with self.assertRaises(SpotifyManagedPlaylistPolicyMismatch):
            adapter.create_private_managed_playlist("syntheticuser", "Synthetic")

    def test_provider_rejection_is_definite_and_bad_201_is_ambiguous_without_identity(self) -> None:
        class Token:
            def access_token(self, *, force_refresh: bool = False) -> str: return "synthetic"
        def rejected(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "synthetic"})
        with httpx.Client(transport=httpx.MockTransport(rejected)) as client:
            adapter = SpotifyManagedPlaylistProvisioner(token_provider=Token(), http_client=client)
            with self.assertRaises(SpotifyAdapterError) as raised:
                adapter.create_private_managed_playlist("syntheticuser", "Synthetic")
        self.assertEqual(SpotifyErrorKind.PROVIDER_REJECTED, raised.exception.kind)
        def malformed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"name": "Synthetic"})
        with httpx.Client(transport=httpx.MockTransport(malformed)) as client:
            adapter = SpotifyManagedPlaylistProvisioner(token_provider=Token(), http_client=client)
            with self.assertRaises(SpotifyAdapterError) as raised:
                adapter.create_private_managed_playlist("syntheticuser", "Synthetic")
        self.assertEqual(SpotifyErrorKind.INVALID_SUCCESS_RESPONSE, raised.exception.kind)


if __name__ == "__main__":
    unittest.main()
