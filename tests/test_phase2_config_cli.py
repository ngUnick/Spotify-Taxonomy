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


def application_payload(**extra: object) -> dict[str, object]:
    return {
        "schema_version": "3",
        "taxonomy_path": "taxonomy.yaml",
        "characteristic_vocabulary_path": "characteristics.yaml",
        "specification_manifest_path": "specifications/manifest.yaml",
        **extra,
    }


class _ContextClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Manager:
    def status(self) -> AuthorizationStatus:
        return AuthorizationStatus(True, ("user-read-private",), datetime(2026, 8, 14, tzinfo=timezone.utc))

    def login(self, *, no_browser: bool = False, managed_provisioning: bool = False) -> AccessToken:
        self.no_browser = no_browser
        self.managed_provisioning = managed_provisioning
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

    def test_config_v3_defaults_and_bounds(self) -> None:
        config = ApplicationConfig.model_validate(application_payload())
        self.assertEqual(20, config.spotify.request_timeout_seconds)
        self.assertEqual(3, config.spotify.max_read_attempts)
        self.assertEqual(300, config.spotify.callback_timeout_seconds)
        self.assertEqual(8888, config.spotify.callback_port)
        configured = ApplicationConfig.model_validate(
            application_payload(spotify={"callback_port": 9443})
        )
        self.assertEqual(9443, configured.spotify.callback_port)
        with self.assertRaises(ValidationError):
            ApplicationConfig.model_validate(
                application_payload(spotify={"max_read_attempts": 11})
            )
        for invalid in (0, 1023, 65536, True, "8888"):
            with self.subTest(callback_port=invalid), self.assertRaises(ValidationError):
                ApplicationConfig.model_validate(
                    application_payload(spotify={"callback_port": invalid})
                )

    def test_login_callback_port_cli_override_precedence(self) -> None:
        configured = ApplicationConfig.model_validate(
            application_payload(spotify={"callback_port": 9443})
        )
        with patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "synthetic-client-id"}), patch(
            "spotify_playlist_taxonomy.cli._load_documents",
            return_value=(configured, object(), Path("synthetic-taxonomy.yaml")),
        ), patch(
            "spotify_playlist_taxonomy.cli._spotify_http_client", return_value=_ContextClient()
        ), patch(
            "spotify_playlist_taxonomy.cli.WindowsCredentialStore", return_value=object()
        ), patch(
            "spotify_playlist_taxonomy.cli.SpotifyAuthManager", return_value=_Manager()
        ) as manager_type:
            configured_result = RUNNER.invoke(
                app, ["spotify", "auth", "login", "--config", str(CONFIG)]
            )
            overridden_result = RUNNER.invoke(app, [
                "spotify", "auth", "login", "--callback-port", "9555",
                "--config", str(CONFIG),
            ])

        self.assertEqual(0, configured_result.exit_code, configured_result.output)
        self.assertEqual(0, overridden_result.exit_code, overridden_result.output)
        self.assertEqual(9443, manager_type.call_args_list[0].kwargs["callback_port"])
        self.assertEqual(9555, manager_type.call_args_list[1].kwargs["callback_port"])

    def test_login_rejects_invalid_cli_callback_port(self) -> None:
        result = RUNNER.invoke(app, [
            "spotify", "auth", "login", "--callback-port", "1023",
            "--config", str(CONFIG),
        ])
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("1024", result.output)

    def test_managed_provisioning_login_is_explicit(self) -> None:
        manager = _Manager()
        with patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "synthetic-client-id"}), patch(
            "spotify_playlist_taxonomy.cli._spotify_http_client", return_value=_ContextClient()
        ), patch("spotify_playlist_taxonomy.cli._spotify_auth_manager", return_value=manager):
            result = RUNNER.invoke(app, ["spotify", "auth", "login", "--managed-provisioning", "--config", str(CONFIG)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertTrue(manager.managed_provisioning)

    def test_secret_and_client_id_fields_remain_forbidden(self) -> None:
        for field in ("client_secret", "client_id", "refresh_token"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ApplicationConfig.model_validate(application_payload(**{field: "synthetic"}))

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
