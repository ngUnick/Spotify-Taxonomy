"""Guarded Phase 11 one-track managed-playlist population."""
from __future__ import annotations

import re
import sqlite3
import uuid
from hashlib import sha256
import json
from dataclasses import dataclass
from typing import Protocol

from spotify_playlist_taxonomy.adapters.spotify.auth import POPULATION_SCOPES, PROVISIONING_SCOPE
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.config import Phase4Bundle
from spotify_playlist_taxonomy.domain.models import IdentityProvider
from spotify_playlist_taxonomy.persistence.database import utc_now
from spotify_playlist_taxonomy.ports.music_platform import MusicPlatformReader
from spotify_playlist_taxonomy.workflows.phase10 import Phase10Service, ProvisioningError
from spotify_playlist_taxonomy.workflows.phase9 import Phase9Service

_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]+$")
_UNRESOLVED = ("started", "recovery_required")
BATCH_POPULATION_MAX_CANONICALS = 100
BATCH_POPULATION_PLAN_VERSION = "population-batch.v1"


class ManagedTrackAdder(Protocol):
    def add_one_managed_track(self, playlist_id: str, provider_track_id: str) -> None: ...


class PopulationError(ValueError):
    pass


class PopulationRecoveryRequired(PopulationError):
    pass


def current_account_canonical_identity_mappings(
    connection: sqlite3.Connection, account_binding_id: str,
    canonical_track_ids: tuple[str, ...] | None = None,
) -> dict[str, tuple[tuple[str, str, str | None], ...]]:
    """Return current identity links for one active account binding only."""

    values: dict[str, list[tuple[str, str, str | None]]] = {}
    if canonical_track_ids is not None and not canonical_track_ids:
        return {}
    query = (
        "SELECT link.canonical_track_id, ti.identity_id, ti.provider, ti.provider_track_id "
        "FROM account_canonical_identity_links AS link "
        "JOIN track_identities AS ti ON ti.identity_id=link.identity_id "
        "JOIN platform_account_bindings AS binding ON binding.binding_id=link.account_binding_id "
        "WHERE link.account_binding_id=? AND binding.active=1"
    )
    params: tuple[object, ...] = (account_binding_id,)
    if canonical_track_ids is not None:
        placeholders = ",".join("?" for _ in canonical_track_ids)
        query += f" AND link.canonical_track_id IN ({placeholders})"
        params = (*params, *canonical_track_ids)
    for row in connection.execute(query + " ORDER BY link.canonical_track_id, ti.identity_id", params):
        values.setdefault(row["canonical_track_id"], []).append((row["identity_id"], row["provider"], row["provider_track_id"]))
    return {key: tuple(value) for key, value in values.items()}


def exact_spotify_population_mappings(
    connection: sqlite3.Connection, account_binding_id: str,
    canonical_track_ids: tuple[str, ...] | None = None,
) -> dict[str, tuple[tuple[str, str | None], ...]]:
    """Return the account-scoped Spotify mappings accepted by guarded population."""

    current = current_account_canonical_identity_mappings(
        connection, account_binding_id, canonical_track_ids,
    )
    return {
        canonical_id: tuple((identity_id, provider_track_id) for identity_id, provider, provider_track_id in rows if provider == IdentityProvider.SPOTIFY.value)
        for canonical_id, rows in current.items()
    }


def is_exact_spotify_population_mapping(rows: tuple[tuple[str, str | None], ...]) -> bool:
    return len(rows) == 1 and isinstance(rows[0][1], str) and bool(_SPOTIFY_ID.fullmatch(rows[0][1]))


@dataclass(frozen=True, slots=True)
class PopulationStatus:
    output_target_id: str
    canonical_track_id: str
    state: str
    reason: str
    membership_count: int | None = None
    required_scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchPopulationPreview:
    plan_id: str
    canonical_track_ids: tuple[str, ...]
    entries: tuple[PopulationStatus, ...]

    @property
    def desired_memberships(self) -> int:
        return len(self.entries)

    def count(self, state: str) -> int:
        return sum(item.state == state for item in self.entries)


@dataclass(frozen=True, slots=True)
class BatchPopulationOutcome:
    plan_id: str
    canonical_track_ids: tuple[str, ...]
    entries: tuple[PopulationStatus, ...]

    def count(self, state: str) -> int:
        return sum(item.state == state for item in self.entries)


@dataclass(frozen=True, slots=True)
class _Preflight:
    target_id: str
    canonical_id: str
    registration_id: str
    playlist_id: str
    identity_id: str
    provider_track_id: str
    membership_count: int
    required_scopes: tuple[str, ...]


class Phase11Service:
    """One explicitly confirmed addition, never a generic synchronization writer."""
    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection, self.bundle = connection, bundle
        self.targets = {item.output_target_id: item for item in bundle.output_targets.targets}
        self.phase9 = Phase9Service(connection, bundle)
        self.phase10 = Phase10Service(connection, bundle)

    def status(self, target_id: str, canonical_id: str) -> PopulationStatus:
        self._target(target_id); self._canonical(canonical_id)
        row = self.connection.execute(
            "SELECT state, readback_membership_count FROM managed_target_addition_attempts WHERE output_target_id=? AND canonical_track_id=? ORDER BY created_at DESC LIMIT 1",
            (target_id, canonical_id),
        ).fetchone()
        if row is None:
            return PopulationStatus(target_id, canonical_id, "no_attempt", "no managed addition attempt is recorded")
        reason = "manual recovery verification is required; automatic retry is disabled" if row["state"] in _UNRESOLVED else "bounded historical managed addition outcome"
        return PopulationStatus(target_id, canonical_id, row["state"], reason, row["readback_membership_count"])

    def preview(self, target_id: str, canonical_id: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader) -> PopulationStatus:
        try:
            item = self._preflight(target_id, canonical_id, account_binding_id=account_binding_id, account_id=account_id, scopes=scopes, reader=reader)
        except PopulationError as error:
            return PopulationStatus(target_id, canonical_id, "blocked", str(error))
        if item.membership_count:
            return PopulationStatus(target_id, canonical_id, "already_present", "exact managed playlist already contains the desired provider track", item.membership_count, item.required_scopes)
        if self._unresolved(target_id, canonical_id) is not None:
            return PopulationStatus(target_id, canonical_id, "recovery_required", "an earlier addition outcome is unresolved; automatic retry is disabled", 0, item.required_scopes)
        return PopulationStatus(target_id, canonical_id, "would_add", "one exact provider track would be appended to the exact managed playlist", 0, item.required_scopes)

    def batch_plan_id(self, canonical_ids: tuple[str, ...]) -> str:
        """Return a deterministic local plan identity; this performs no provider I/O or writes."""

        canonical_ids = self._batch_canonical_ids(canonical_ids)
        memberships = tuple(
            (state.canonical_track_id, item.output_target_id)
            for state in self.phase9.desired_many(canonical_ids)
            for item in state.memberships
        )
        payload = json.dumps(
            {
                "version": BATCH_POPULATION_PLAN_VERSION,
                "canonical_ids": canonical_ids,
                "memberships": memberships,
                "taxonomy_version": self.bundle.taxonomy.taxonomy_version,
                "bundle_digest": self.bundle.digest,
            },
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        return sha256(payload).hexdigest()[:16]

    def batch_preview(self, canonical_ids: tuple[str, ...], *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader) -> BatchPopulationPreview:
        """Run the ordinary exact live preflight for every current desired membership."""

        canonical_ids = self._batch_canonical_ids(canonical_ids)
        plan_id = self.batch_plan_id(canonical_ids)
        entries = tuple(
            self.preview(membership.output_target_id, state.canonical_track_id,
                         account_binding_id=account_binding_id, account_id=account_id,
                         scopes=scopes, reader=reader)
            for state in self.phase9.desired_many(canonical_ids)
            for membership in state.memberships
        )
        return BatchPopulationPreview(plan_id, canonical_ids, entries)

    def batch_apply(self, canonical_ids: tuple[str, ...], confirmation: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader, adder: ManagedTrackAdder) -> BatchPopulationOutcome:
        """Revalidate a bounded batch, then preserve the existing one-track guarded write path."""

        canonical_ids = self._batch_canonical_ids(canonical_ids)
        expected_plan_id = self.batch_plan_id(canonical_ids)
        if confirmation != f"APPLY:{expected_plan_id}":
            raise PopulationError("explicit confirmation must equal APPLY:<batch-plan-id>")
        preview = self.batch_preview(
            canonical_ids, account_binding_id=account_binding_id, account_id=account_id,
            scopes=scopes, reader=reader,
        )
        entries: list[PopulationStatus] = []
        pending = list(preview.entries)
        while pending:
            item = pending.pop(0)
            if item.state != "would_add":
                entries.append(item)
                continue
            try:
                # `apply` records its attempt before the outbound request and verifies read-back afterwards.
                entries.append(self.apply(
                    item.output_target_id, item.canonical_track_id,
                    f"ADD:{item.output_target_id}:{item.canonical_track_id}",
                    account_binding_id=account_binding_id, account_id=account_id,
                    scopes=scopes, reader=reader, adder=adder,
                ))
            except PopulationRecoveryRequired as error:
                entries.append(PopulationStatus(item.output_target_id, item.canonical_track_id, "recovery_required", str(error)))
                entries.extend(
                    PopulationStatus(next_item.output_target_id, next_item.canonical_track_id, "not_attempted",
                                     "not attempted after an ambiguous outbound addition; recovery verification is required")
                    for next_item in pending if next_item.state == "would_add"
                )
                entries.extend(next_item for next_item in pending if next_item.state != "would_add")
                break
            except PopulationError as error:
                # A definitively rejected request remains isolated; unrelated safe items may proceed.
                entries.append(PopulationStatus(item.output_target_id, item.canonical_track_id, "blocked", str(error)))
        return BatchPopulationOutcome(preview.plan_id, canonical_ids, tuple(entries))

    def apply(self, target_id: str, canonical_id: str, confirmation: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader, adder: ManagedTrackAdder) -> PopulationStatus:
        if confirmation != f"ADD:{target_id}:{canonical_id}":
            raise PopulationError("explicit confirmation must equal ADD:<target-id>:<canonical-id>")
        item = self._preflight(target_id, canonical_id, account_binding_id=account_binding_id, account_id=account_id, scopes=scopes, reader=reader)
        if item.membership_count:
            return PopulationStatus(target_id, canonical_id, "already_present", "exact managed playlist already contains the desired provider track", item.membership_count, item.required_scopes)
        if self._unresolved(target_id, canonical_id) is not None:
            raise PopulationRecoveryRequired("an earlier addition outcome is unresolved; automatic retry is disabled")
        attempt_id = f"addition.{uuid.uuid4().hex}"
        try:
            self.connection.execute(
                "INSERT INTO managed_target_addition_attempts (attempt_id, output_target_id, canonical_track_id, identity_id, registration_id, state, confirmation, created_at) VALUES (?, ?, ?, ?, ?, 'started', ?, ?)",
                (attempt_id, target_id, canonical_id, item.identity_id, item.registration_id, confirmation, utc_now()),
            )
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise PopulationError("unable to record managed addition attempt before dispatch") from error
        try:
            adder.add_one_managed_track(item.playlist_id, item.provider_track_id)
        except SpotifyAdapterError as error:
            if error.kind in {SpotifyErrorKind.REQUEST_NOT_SENT, SpotifyErrorKind.PROVIDER_REJECTED}:
                state = "request_not_sent" if error.kind is SpotifyErrorKind.REQUEST_NOT_SENT else "provider_rejected"
                self._finalize(attempt_id, state, acknowledged=False, count=None)
                raise PopulationError(str(error)) from None
            self._recover(attempt_id, None)
            raise PopulationRecoveryRequired(str(error)) from None
        try:
            count = self._membership_count(item.playlist_id, item.provider_track_id, reader)
        except Exception:
            self._recover(attempt_id, None)
            raise PopulationRecoveryRequired("provider_acknowledged_readback_failed: automatic retry is disabled") from None
        if count != 1:
            self._recover(attempt_id, count)
            detail = "provider_acknowledged_membership_absent" if count == 0 else "provider_acknowledged_duplicate_membership"
            raise PopulationRecoveryRequired(f"{detail}: automatic retry is disabled")
        try:
            self._finalize(attempt_id, "confirmed_added", acknowledged=True, count=1)
        except PopulationError:
            raise PopulationRecoveryRequired("local_finalization_failed_after_provider_confirmation: recovery verification is required") from None
        return PopulationStatus(target_id, canonical_id, "confirmed_added", "one exact provider track was confirmed present by read-back", 1, item.required_scopes)

    def recover_verify(self, target_id: str, canonical_id: str, confirmation: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader) -> PopulationStatus:
        if confirmation != f"RECOVER:{target_id}:{canonical_id}":
            raise PopulationError("explicit recovery confirmation must equal RECOVER:<target-id>:<canonical-id>")
        attempt = self._unresolved(target_id, canonical_id)
        if attempt is None:
            raise PopulationError("no unresolved managed addition attempt exists")
        item = self._preflight(target_id, canonical_id, account_binding_id=account_binding_id, account_id=account_id, scopes=scopes, reader=reader)
        if item.membership_count == 1:
            self._finalize(attempt["attempt_id"], "confirmed_recovered", acknowledged=True, count=1)
            return PopulationStatus(target_id, canonical_id, "confirmed_recovered", "exact read-back confirmed one provider membership; no provider write occurred", 1, item.required_scopes)
        self._recover(attempt["attempt_id"], item.membership_count)
        reason = "membership remains absent" if item.membership_count == 0 else "duplicate membership remains unresolved"
        return PopulationStatus(target_id, canonical_id, "recovery_required", f"{reason}; automatic retry and cleanup are disabled", item.membership_count, item.required_scopes)

    def _preflight(self, target_id: str, canonical_id: str, *, account_binding_id: str | None, account_id: str | None, scopes: tuple[str, ...], reader: MusicPlatformReader) -> _Preflight:
        self._target(target_id); self._canonical(canonical_id)
        if target_id not in {item.output_target_id for item in self.phase9.desired(canonical_id).memberships}:
            raise PopulationError("canonical track does not have an effective desired membership for this target")
        registration = self.connection.execute("SELECT * FROM managed_target_registrations WHERE output_target_id=?", (target_id,)).fetchone()
        if registration is None or registration["state"] != "active" or registration["provider_playlist_id"] is None:
            raise PopulationError("no active exact managed registration exists")
        if registration["policy_confirmed"] != 1:
            raise PopulationError("managed registration is not policy-confirmed")
        if account_binding_id is None or account_binding_id != registration["account_binding_id"]:
            raise PopulationError("managed registration belongs to a different active account binding")
        binding = self.connection.execute("SELECT account_id FROM platform_account_bindings WHERE binding_id=? AND active=1", (account_binding_id,)).fetchone()
        if binding is None or account_id is None or binding["account_id"] != account_id:
            raise PopulationError("authenticated provider account does not match the active local binding")
        try:
            policy = self.phase10.verify_policy(target_id, reader)
        except ProvisioningError as error:
            raise PopulationError(str(error)) from None
        if not policy.policy_confirmed:
            raise PopulationError("provider collaboration policy is not confirmed")
        required = self._required_scopes(policy.public_state)
        if not set(required).issubset(scopes):
            raise PopulationError("reauthorization with --managed-population is required for this provider playlist publication state")
        identity_id, provider_track_id = self._mapping(canonical_id, account_binding_id)
        count = self._membership_count(registration["provider_playlist_id"], provider_track_id, reader)
        return _Preflight(target_id, canonical_id, registration["registration_id"], registration["provider_playlist_id"], identity_id, provider_track_id, count, required)

    def _mapping(self, canonical_id: str, account_binding_id: str) -> tuple[str, str]:
        rows = exact_spotify_population_mappings(
            self.connection, account_binding_id, (canonical_id,),
        ).get(canonical_id, ())
        if len(rows) != 1:
            raise PopulationError("canonical track requires exactly one Spotify provider mapping")
        identity_id, track_id = rows[0]
        if not isinstance(track_id, str) or not _SPOTIFY_ID.fullmatch(track_id):
            raise PopulationError("canonical track Spotify provider mapping is invalid")
        return identity_id, track_id

    def _membership_count(self, playlist_id: str, provider_track_id: str, reader: MusicPlatformReader) -> int:
        cursor: str | None = None; seen: set[str] = set(); count = 0
        while True:
            if cursor is not None:
                if cursor in seen: raise PopulationError("playlist membership pagination did not complete")
                seen.add(cursor)
            page = reader.read_playlist_items(playlist_id, cursor)
            if page.issues:
                raise PopulationError("playlist membership cannot be counted safely")
            for track in page.items:
                if track.provider_track_id == provider_track_id or track.linked_from_provider_track_id == provider_track_id:
                    count += 1
            if page.next_cursor is None:
                return count
            cursor = page.next_cursor

    @staticmethod
    def _required_scopes(public_state: str) -> tuple[str, ...]:
        if public_state == "true": return ("playlist-modify-public",)
        if public_state == "false": return (PROVISIONING_SCOPE,)
        return POPULATION_SCOPES

    def _target(self, target_id: str) -> None:
        if target_id not in self.targets: raise PopulationError("unknown concrete output target")

    def _canonical(self, canonical_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM canonical_tracks WHERE canonical_track_id=?", (canonical_id,)).fetchone() is None:
            raise PopulationError("unknown canonical track")

    def _batch_canonical_ids(self, canonical_ids: tuple[str, ...]) -> tuple[str, ...]:
        if not canonical_ids or len(canonical_ids) > BATCH_POPULATION_MAX_CANONICALS:
            raise PopulationError(f"batch population requires 1-{BATCH_POPULATION_MAX_CANONICALS} distinct canonical IDs")
        if len(set(canonical_ids)) != len(canonical_ids):
            raise PopulationError("batch population contains duplicate canonical IDs")
        values = tuple(sorted(canonical_ids))
        for canonical_id in values:
            self._canonical(canonical_id)
        return values

    def _unresolved(self, target_id: str, canonical_id: str):
        return self.connection.execute("SELECT * FROM managed_target_addition_attempts WHERE output_target_id=? AND canonical_track_id=? AND state IN ('started', 'recovery_required') ORDER BY created_at DESC LIMIT 1", (target_id, canonical_id)).fetchone()

    def _finalize(self, attempt_id: str, state: str, *, acknowledged: bool, count: int | None) -> None:
        try:
            result = self.connection.execute("UPDATE managed_target_addition_attempts SET state=?, provider_acknowledged=?, readback_membership_count=?, completed_at=? WHERE attempt_id=? AND state IN ('started', 'recovery_required')", (state, int(acknowledged), count, utc_now(), attempt_id))
            if result.rowcount != 1: raise sqlite3.DatabaseError("managed addition attempt changed")
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise PopulationError("unable to finalize managed addition audit") from error

    def _recover(self, attempt_id: str, count: int | None) -> None:
        try:
            self._finalize(attempt_id, "recovery_required", acknowledged=True, count=count)
        except PopulationError:
            # The committed started attempt deliberately remains a retry blocker.
            pass
