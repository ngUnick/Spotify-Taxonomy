"""Protected refresh-token storage using only keyring's core Windows backend."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys
from typing import Protocol, runtime_checkable

from .errors import SpotifyAdapterError, SpotifyErrorKind

SERVICE_NAME = "SpotifyPlaylistTaxonomy"
ENVELOPE_VERSION = 1


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    refresh_token: str
    scopes: tuple[str, ...]
    authorized_at: datetime

    def __post_init__(self) -> None:
        if not self.refresh_token:
            raise ValueError("refresh_token must not be blank")
        if not self.scopes or any(not scope for scope in self.scopes):
            raise ValueError("scopes must contain nonblank values")
        if self.authorized_at.tzinfo is None:
            raise ValueError("authorized_at must be timezone-aware")


@runtime_checkable
class CredentialStore(Protocol):
    def load(self, client_id: str) -> CredentialEnvelope | None: ...

    def save(self, client_id: str, credential: CredentialEnvelope) -> None: ...

    def delete(self, client_id: str) -> bool: ...


class _WindowsBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def credential_slot(client_id: str) -> str:
    if not client_id.strip():
        raise SpotifyAdapterError(SpotifyErrorKind.CONFIGURATION, "Spotify Client ID is required")
    digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()
    return f"spotify-pkce-{digest}"


def _core_windows_backend() -> _WindowsBackend:
    """Resolve a concrete backend defined by the installed keyring core package."""

    if sys.platform != "win32":
        raise SpotifyAdapterError(
            SpotifyErrorKind.CREDENTIAL_BACKEND,
            "The approved Windows Credential Locker backend is unavailable",
        )
    try:
        keyring_package = importlib.import_module("keyring")
        backend_base = importlib.import_module("keyring.backend").KeyringBackend
        windows_module = importlib.import_module("keyring.backends.Windows")
        package_root = Path(keyring_package.__file__).resolve().parent
        module_path = Path(windows_module.__file__).resolve()
        if not module_path.is_relative_to(package_root):
            raise RuntimeError("Windows backend is not supplied by keyring core")

        preferred = getattr(windows_module, "WinVaultKeyring", None)
        candidates = [
            value
            for value in vars(windows_module).values()
            if isinstance(value, type)
            and issubclass(value, backend_base)
            and value is not backend_base
            and value.__module__ == windows_module.__name__
        ]
        if preferred in candidates:
            backend_class = preferred
        elif len(candidates) == 1:
            # Supports a future core rename without accepting a plugin class.
            backend_class = candidates[0]
        else:
            raise RuntimeError("No unambiguous core Windows backend implementation")

        backend = backend_class()
        for operation in ("get_password", "set_password", "delete_password"):
            if not callable(getattr(backend, operation, None)):
                raise RuntimeError("Core Windows backend lacks required capability")
        return backend
    except SpotifyAdapterError:
        raise
    except Exception:
        raise SpotifyAdapterError(
            SpotifyErrorKind.CREDENTIAL_BACKEND,
            "The approved Windows Credential Locker backend is unavailable",
        ) from None


class WindowsCredentialStore:
    """Fail-closed credential storage using keyring's built-in WinVault adapter."""

    def __init__(self) -> None:
        self._backend = _core_windows_backend()

    def load(self, client_id: str) -> CredentialEnvelope | None:
        try:
            payload = self._backend.get_password(SERVICE_NAME, credential_slot(client_id))
            if payload is None:
                return None
            return _decode_envelope(payload)
        except SpotifyAdapterError:
            raise
        except Exception:
            raise SpotifyAdapterError(
                SpotifyErrorKind.CREDENTIAL_BACKEND,
                "Unable to read protected Spotify credentials",
            ) from None

    def save(self, client_id: str, credential: CredentialEnvelope) -> None:
        try:
            self._backend.set_password(
                SERVICE_NAME, credential_slot(client_id), _encode_envelope(credential)
            )
        except SpotifyAdapterError:
            raise
        except Exception:
            raise SpotifyAdapterError(
                SpotifyErrorKind.CREDENTIAL_BACKEND,
                "Unable to store protected Spotify credentials",
            ) from None

    def delete(self, client_id: str) -> bool:
        try:
            slot = credential_slot(client_id)
            if self._backend.get_password(SERVICE_NAME, slot) is None:
                return False
            self._backend.delete_password(SERVICE_NAME, slot)
            return True
        except SpotifyAdapterError:
            raise
        except Exception:
            raise SpotifyAdapterError(
                SpotifyErrorKind.CREDENTIAL_BACKEND,
                "Unable to delete protected Spotify credentials",
            ) from None


def _encode_envelope(credential: CredentialEnvelope) -> str:
    return json.dumps(
        {
            "version": ENVELOPE_VERSION,
            "refresh_token": credential.refresh_token,
            "scopes": list(credential.scopes),
            "authorized_at": credential.authorized_at.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_envelope(payload: str) -> CredentialEnvelope:
    try:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {
            "version", "refresh_token", "scopes", "authorized_at"
        }:
            raise ValueError("invalid credential envelope shape")
        if value["version"] != ENVELOPE_VERSION:
            raise ValueError("unsupported credential envelope version")
        if not isinstance(value["refresh_token"], str):
            raise ValueError("invalid refresh token")
        scopes = value["scopes"]
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise ValueError("invalid scopes")
        if not isinstance(value["authorized_at"], str):
            raise ValueError("invalid authorization timestamp")
        return CredentialEnvelope(
            refresh_token=value["refresh_token"],
            scopes=tuple(scopes),
            authorized_at=datetime.fromisoformat(value["authorized_at"]),
        )
    except Exception:
        raise SpotifyAdapterError(
            SpotifyErrorKind.CREDENTIAL_BACKEND,
            "Protected Spotify credentials are invalid",
        ) from None
