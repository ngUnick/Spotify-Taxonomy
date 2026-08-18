"""Purpose-specific Phase 10 managed target provisioning; no track population."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Protocol

from spotify_playlist_taxonomy.adapters.spotify.provisioning import (
    PROVISIONING_SCOPE, SpotifyManagedPlaylistPolicyMismatch,
)
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.ports.music_platform import MusicPlatformReader
from spotify_playlist_taxonomy.persistence.database import utc_now


class ManagedPlaylistCreator(Protocol):
    def create_private_managed_playlist(self, account_id: str, name: str) -> str: ...


class ProvisioningError(ValueError):
    pass


class RecoveryRequiredError(ProvisioningError):
    """Remote creation may have happened; automatic retry is forbidden."""
    pass


ABANDON_UNKNOWN_PREFIX = "ABANDON-UNKNOWN:"

@dataclass(frozen=True, slots=True)
class ProvisioningStatus:
    output_target_id: str
    state: str
    provider_playlist_id: str | None
    account_binding_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class PolicyVerification:
    output_target_id: str
    policy_confirmed: bool
    name_matches: bool
    description_matches: bool
    public_state: str
    collaborative_state: str


class Phase10Service:
    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.targets = {item.output_target_id: item for item in bundle.output_targets.targets}

    def status(self, target_id: str) -> ProvisioningStatus:
        self._target(target_id)
        row = self.connection.execute("SELECT * FROM managed_target_registrations WHERE output_target_id=?", (target_id,)).fetchone()
        if row is None:
            abandoned = self.connection.execute(
                "SELECT COUNT(*) AS count FROM managed_target_abandoned_unknown_attempts WHERE output_target_id=?",
                (target_id,),
            ).fetchone()["count"]
            reason = "no durable managed registration" if not abandoned else "prior ambiguous attempt explicitly abandoned; provisioning is retry-eligible"
            return ProvisioningStatus(target_id, "unprovisioned", None, None, reason)
        if row["state"] == "active":
            reason = "provider-confirmed non-collaborative managed playlist is durably registered" if row["policy_confirmed"] else "durably registered managed playlist; provider collaboration policy is not confirmed"
        else:
            reason = "manual recovery is required; automatic retry is disabled"
        return ProvisioningStatus(target_id, row["state"], row["provider_playlist_id"], row["account_binding_id"], reason)

    def preview(self, target_id: str, *, account_binding_id: str | None, scopes: tuple[str, ...]) -> ProvisioningStatus:
        target = self._target(target_id)
        existing = self.status(target_id)
        if existing.state != "unprovisioned":
            if account_binding_id is not None and existing.account_binding_id != account_binding_id:
                return ProvisioningStatus(target_id, "blocked", None, existing.account_binding_id, "managed registration belongs to a different account binding")
            return existing
        if account_binding_id is None: return ProvisioningStatus(target_id, "blocked", None, None, "no active provider account binding")
        if not self._active_binding(account_binding_id):
            return ProvisioningStatus(target_id, "blocked", None, account_binding_id, "specified provider account binding is not active")
        if PROVISIONING_SCOPE not in scopes: return ProvisioningStatus(target_id, "blocked", None, account_binding_id, "reauthorization with playlist-modify-private is required")
        return ProvisioningStatus(target_id, "preview", None, account_binding_id, f"would create public=false non-collaborative playlist named {target.display_name}")

    def apply(self, target_id: str, confirmation: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], creator: ManagedPlaylistCreator) -> ProvisioningStatus:
        if confirmation != target_id:
            raise ProvisioningError("explicit confirmation must equal the concrete target ID")
        preview = self.preview(target_id, account_binding_id=account_binding_id, scopes=scopes)
        if preview.state == "active":
            if preview.account_binding_id != account_binding_id: raise ProvisioningError("managed registration belongs to a different account binding")
            return preview
        if preview.state != "preview" or account_id is None:
            raise ProvisioningError(preview.reason)
        binding = self._active_binding(account_binding_id)
        if binding is None or binding["account_id"] != account_id:
            raise ProvisioningError("authenticated provider account does not match the active local binding")
        target = self._target(target_id)
        attempt = f"managed.{uuid.uuid4().hex}"
        try:
            self.connection.execute(
                "INSERT INTO managed_target_registrations "
                "(output_target_id, registration_id, provider, account_binding_id, provider_playlist_id, state, provenance, created_at, registered_at) "
                "VALUES (?, ?, 'spotify', ?, NULL, 'provisioning', 'phase10_create', ?, NULL)",
                (target_id, attempt, account_binding_id, utc_now()),
            )
            self.connection.commit()
        except sqlite3.Error as error: raise ProvisioningError("unable to record provisioning attempt") from error
        try:
            playlist_id = creator.create_private_managed_playlist(account_id, target.display_name)
        except SpotifyManagedPlaylistPolicyMismatch as error:
            self._mark_recovery_required(target_id, error.playlist_id)
            raise RecoveryRequiredError("provider_policy_mismatch: provider-confirmed playlist requires manual recovery") from None
        except Exception as error:
            if self._definitely_not_created(error):
                self._discard_unstarted_attempt(target_id)
                raise ProvisioningError(self._sanitized_failure(error)) from None
            self._mark_recovery_required(target_id)
            raise RecoveryRequiredError(self._sanitized_failure(error)) from None
        try:
            result = self.connection.execute("UPDATE managed_target_registrations SET provider_playlist_id=?, state='active', policy_confirmed=1, registered_at=? WHERE output_target_id=? AND state='provisioning'", (playlist_id, utc_now(), target_id))
            if result.rowcount != 1:
                raise sqlite3.DatabaseError("provisioning attempt was not active")
            self.connection.commit()
        except sqlite3.Error as error:
            self._mark_recovery_required(target_id, playlist_id)
            raise RecoveryRequiredError("local_registration_failed_after_remote_success: manual recovery is required") from None
        return self.status(target_id)

    def abandon_unknown(self, target_id: str, confirmation: str) -> ProvisioningStatus:
        """Explicitly preserve and abandon one unknown remote outcome without provider access."""
        self._target(target_id)
        expected = f"{ABANDON_UNKNOWN_PREFIX}{target_id}"
        if confirmation != expected:
            raise ProvisioningError("explicit abandonment confirmation does not match the concrete target ID")
        row = self.connection.execute(
            "SELECT * FROM managed_target_registrations WHERE output_target_id=?",
            (target_id,),
        ).fetchone()
        if row is None or row["state"] != "recovery_required" or row["provider_playlist_id"] is not None:
            raise ProvisioningError("only an unknown recovery-required attempt can be explicitly abandoned")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO managed_target_abandoned_unknown_attempts "
                "(history_id, output_target_id, registration_id, provider, account_binding_id, provider_playlist_id, original_state, outcome_state, provenance, abandoned_at, confirmation) "
                "VALUES (?, ?, ?, ?, ?, NULL, 'recovery_required', 'abandoned_unknown', 'operator_abandon_unknown.v1', ?, ?)",
                (f"abandoned.{uuid.uuid4().hex}", target_id, row["registration_id"], row["provider"], row["account_binding_id"], utc_now(), confirmation),
            )
            deleted = self.connection.execute(
                "DELETE FROM managed_target_registrations WHERE output_target_id=? AND registration_id=? AND state='recovery_required' AND provider_playlist_id IS NULL",
                (target_id, row["registration_id"]),
            )
            if deleted.rowcount != 1:
                raise sqlite3.DatabaseError("recovery attempt changed before abandonment")
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ProvisioningError("unable to preserve the abandoned unknown attempt") from None
        return self.status(target_id)

    def verify_policy(self, target_id: str, reader: MusicPlatformReader) -> PolicyVerification:
        """Read only the exact registered playlist; never search or adopt a provider object."""
        target = self._target(target_id)
        registration = self.connection.execute(
            "SELECT * FROM managed_target_registrations WHERE output_target_id=? AND state='active'",
            (target_id,),
        ).fetchone()
        if registration is None or registration["provider_playlist_id"] is None:
            raise ProvisioningError("an active exact managed registration is required for policy verification")
        binding = self.connection.execute(
            "SELECT account_id, current_user_id FROM platform_account_bindings WHERE binding_id=? AND active=1",
            (registration["account_binding_id"],),
        ).fetchone()
        if binding is None:
            raise ProvisioningError("registered provider account binding is not active")
        account = reader.read_current_account()
        if account.account_id != binding["account_id"]:
            raise ProvisioningError("authenticated provider account does not match the managed registration")
        playlist = reader.read_playlist(registration["provider_playlist_id"])
        if playlist.playlist_id != registration["provider_playlist_id"]:
            raise ProvisioningError("provider returned a different playlist identity")
        if playlist.owner_user_id != binding["current_user_id"] or playlist.owner_user_id != account.spotify_user_id:
            raise ProvisioningError("provider playlist owner does not match the managed account binding")
        public_state = "null" if playlist.public is None else str(playlist.public).lower()
        collaborative_state = str(playlist.collaborative).lower()
        result = PolicyVerification(
            target_id, playlist.collaborative is False,
            playlist.name == target.display_name, playlist.description == "Managed by Spotify Taxonomy.",
            public_state, collaborative_state,
        )
        if not result.policy_confirmed:
            return result
        try:
            updated = self.connection.execute(
                "UPDATE managed_target_registrations SET policy_confirmed=1 WHERE output_target_id=? AND registration_id=? AND state='active'",
                (target_id, registration["registration_id"]),
            )
            if updated.rowcount != 1:
                raise sqlite3.DatabaseError("managed registration changed during verification")
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise ProvisioningError("unable to persist provider policy confirmation") from None
        return result

    def _target(self, target_id: str):
        target = self.targets.get(target_id)
        if target is None: raise ProvisioningError("unknown concrete output target")
        return target

    def _active_binding(self, binding_id: str | None):
        if binding_id is None:
            return None
        return self.connection.execute(
            "SELECT binding_id, account_id FROM platform_account_bindings WHERE binding_id=? AND active=1",
            (binding_id,),
        ).fetchone()

    def _discard_unstarted_attempt(self, target_id: str) -> None:
        try:
            self.connection.execute(
                "DELETE FROM managed_target_registrations WHERE output_target_id=? AND state='provisioning'",
                (target_id,),
            )
            self.connection.commit()
        except sqlite3.Error:
            # A durable attempt remains: fail closed rather than permit a retry.
            self.connection.rollback()
            self._mark_recovery_required(target_id)

    def _mark_recovery_required(self, target_id: str, provider_playlist_id: str | None = None) -> None:
        try:
            self.connection.execute(
                "UPDATE managed_target_registrations SET state='recovery_required', provider_playlist_id=COALESCE(?, provider_playlist_id) WHERE output_target_id=?",
                (provider_playlist_id, target_id),
            )
            self.connection.commit()
        except sqlite3.Error:
            # The committed provisioning attempt still blocks automatic retries.
            self.connection.rollback()

    @staticmethod
    def _definitely_not_created(error: Exception) -> bool:
        if not isinstance(error, SpotifyAdapterError):
            return False
        return error.kind in {
            SpotifyErrorKind.CONFIGURATION,
            SpotifyErrorKind.CREDENTIAL_BACKEND,
            SpotifyErrorKind.NOT_AUTHORIZED,
            SpotifyErrorKind.AUTHORIZATION_DENIED,
            SpotifyErrorKind.AUTHORIZATION_FAILED,
            SpotifyErrorKind.REQUEST_NOT_SENT,
            SpotifyErrorKind.PROVIDER_REJECTED,
        }

    @staticmethod
    def _sanitized_failure(error: Exception) -> str:
        if isinstance(error, SpotifyAdapterError):
            return str(error)
        return "remote_outcome_unknown: managed playlist creation outcome is unknown"
