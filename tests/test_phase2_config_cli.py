from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from pydantic import ValidationError
from typer.testing import CliRunner

from spotify_playlist_taxonomy.adapters.spotify.auth import AccessToken, AuthorizationStatus
from spotify_playlist_taxonomy.config import ApplicationConfig
from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.ports.music_platform import PlatformAccountIdentity

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
RUNNER = CliRunner()


class _ContextClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Manager:
    def status(self) -> AuthorizationStatus:
        return AuthorizationStatus(True, ("user-read-private",), datetime(2026, 8, 14, tzinfo=timezone.utc))

    def login(self, *, no_browser: bool = False) -> AccessToken:
        self.no_browser = no_browser
        return AccessToken(
            "synthetic-access",
            datetime(2026, 8, 14, 1, tzinfo=timezone.utc),
            ("user-read-private",),
        )

    def logout(self) -> bool:
        return True


class _Reader:
    def __init__(self, **kwargs) -> None:
        pass

    def read_current_account(self) -> PlatformAccountIdentity:
        return PlatformAccountIdentity("synthetic-account-id", "synthetic-user-id")


class Phase2ConfigurationCliTests(unittest.TestCase):
    def invoke_with_fake_services(self, arguments: list[str]):
        with patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "synthetic-client-id"}), patch(
            "spotify_playlist_taxonomy.cli._spotify_http_client", return_value=_ContextClient()
        ), patch(
            "spotify_playlist_taxonomy.cli._spotify_auth_manager", return_value=_Manager()
        ), patch(
            "spotify_playlist_taxonomy.cli.SpotifyReadOnlyAdapter", _Reader
        ):
            return RUNNER.invoke(app, arguments)

    def test_config_v2_defaults_and_bounds(self) -> None:
        config = ApplicationConfig.model_validate({
            "schema_version": "2", "taxonomy_path": "taxonomy.yaml"
        })
        self.assertEqual(20, config.spotify.request_timeout_seconds)
        self.assertEqual(3, config.spotify.max_read_attempts)
        self.assertEqual(300, config.spotify.callback_timeout_seconds)
        with self.assertRaises(ValidationError):
            ApplicationConfig.model_validate({
                "schema_version": "2",
                "taxonomy_path": "taxonomy.yaml",
                "spotify": {"max_read_attempts": 11},
            })

    def test_secret_and_client_id_fields_remain_forbidden(self) -> None:
        for field in ("client_secret", "client_id", "refresh_token"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ApplicationConfig.model_validate({
                    "schema_version": "2", "taxonomy_path": "taxonomy.yaml", field: "synthetic"
                })

    def test_spotify_help_is_local(self) -> None:
        result = RUNNER.invoke(app, ["spotify", "--help"])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("auth", result.output)
        self.assertIn("check", result.output)

    def test_spotify_check_is_synthetic_and_masks_identifiers(self) -> None:
        result = self.invoke_with_fake_services(
            ["spotify", "check", "--config", str(CONFIG)]
        )
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("synt…t-id", result.output)
        self.assertNotIn("synthetic-account-id", result.output)
        self.assertNotIn("synthetic-user-id", result.output)
        self.assertNotIn("synthetic-client-id", result.output)

    def test_auth_status_and_logout_are_local_and_mask_client_id(self) -> None:
        status = self.invoke_with_fake_services(
            ["spotify", "auth", "status", "--config", str(CONFIG)]
        )
        logout = self.invoke_with_fake_services(
            ["spotify", "auth", "logout", "--yes", "--config", str(CONFIG)]
        )
        self.assertEqual(0, status.exit_code, status.output)
        self.assertIn("authorized: True", status.output)
        self.assertEqual(0, logout.exit_code, logout.output)
        self.assertIn("Local credential removed: True", logout.output)
        self.assertIn("revoke", logout.output.lower())
        self.assertNotIn("synthetic-client-id", status.output + logout.output)


if __name__ == "__main__":
    unittest.main()
