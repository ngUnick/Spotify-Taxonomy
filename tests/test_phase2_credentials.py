from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.adapters.spotify.credentials import (
    CredentialEnvelope,
    SERVICE_NAME,
    WindowsCredentialStore,
    _core_windows_backend,
    credential_slot,
)
from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError

CLIENT_ID = "synthetic-public-client-id"
NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


class CredentialTests(unittest.TestCase):
    def test_round_trip_uses_hashed_slot_and_minimal_envelope(self) -> None:
        backend = FakeBackend()
        with patch(
            "spotify_playlist_taxonomy.adapters.spotify.credentials._core_windows_backend",
            return_value=backend,
        ):
            store = WindowsCredentialStore()
        credential = CredentialEnvelope("synthetic-refresh", ("user-read-private",), NOW)
        store.save(CLIENT_ID, credential)
        slot = credential_slot(CLIENT_ID)
        self.assertNotIn(CLIENT_ID, slot)
        payload = backend.values[(SERVICE_NAME, slot)]
        self.assertNotIn("access_token", payload)
        self.assertNotIn("client_id", payload)
        self.assertEqual(credential, store.load(CLIENT_ID))
        self.assertTrue(store.delete(CLIENT_ID))
        self.assertFalse(store.delete(CLIENT_ID))

    def test_invalid_envelope_fails_closed(self) -> None:
        backend = FakeBackend()
        backend.values[(SERVICE_NAME, credential_slot(CLIENT_ID))] = '{"version":999}'
        with patch(
            "spotify_playlist_taxonomy.adapters.spotify.credentials._core_windows_backend",
            return_value=backend,
        ), self.assertRaises(SpotifyAdapterError):
            WindowsCredentialStore().load(CLIENT_ID)

    def test_backend_operation_failure_is_sanitized(self) -> None:
        class FailingBackend(FakeBackend):
            def get_password(self, service: str, username: str) -> str | None:
                raise RuntimeError("backend internals")

        with patch(
            "spotify_playlist_taxonomy.adapters.spotify.credentials._core_windows_backend",
            return_value=FailingBackend(),
        ), self.assertRaises(SpotifyAdapterError) as raised:
            WindowsCredentialStore().load(CLIENT_ID)
        self.assertNotIn("backend internals", str(raised.exception))

    def test_core_backend_is_verified_by_class_and_package_location(self) -> None:
        class BackendBase:
            pass

        class CoreBackend(BackendBase, FakeBackend):
            def __init__(self) -> None:
                FakeBackend.__init__(self)

        CoreBackend.__module__ = "keyring.backends.Windows"
        package = SimpleNamespace(__file__=str(Path("C:/synthetic/keyring/__init__.py")))
        base_module = SimpleNamespace(KeyringBackend=BackendBase)
        windows = SimpleNamespace(
            __name__="keyring.backends.Windows",
            __file__=str(Path("C:/synthetic/keyring/backends/Windows.py")),
            WinVaultKeyring=CoreBackend,
            CoreBackend=CoreBackend,
        )
        modules = {
            "keyring": package,
            "keyring.backend": base_module,
            "keyring.backends.Windows": windows,
        }
        with patch("sys.platform", "win32"), patch(
            "spotify_playlist_taxonomy.adapters.spotify.credentials.importlib.import_module",
            side_effect=lambda name: modules[name],
        ):
            backend = _core_windows_backend()
        self.assertIsInstance(backend, CoreBackend)

    def test_third_party_or_non_windows_backend_is_rejected(self) -> None:
        with patch("sys.platform", "linux"):
            with self.assertRaises(SpotifyAdapterError):
                _core_windows_backend()


if __name__ == "__main__":
    unittest.main()
