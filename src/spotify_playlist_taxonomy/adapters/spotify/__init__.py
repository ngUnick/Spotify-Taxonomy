"""Spotify PKCE and read-only Web API adapter."""

from .auth import (
    REQUIRED_SCOPES,
    AccessToken,
    SpotifyAuthManager,
    mask_identifier,
    resolve_client_id,
)
from .credentials import CredentialEnvelope, CredentialStore, WindowsCredentialStore
from .errors import SpotifyAdapterError, SpotifyErrorKind
from .reader import SpotifyReadOnlyAdapter, TokenProvider
from .provisioning import (
    MANAGED_DESCRIPTION, PROVISIONING_SCOPE, SpotifyManagedPlaylistPolicyMismatch,
    SpotifyManagedPlaylistProvisioner,
)

__all__ = [
    "REQUIRED_SCOPES", "AccessToken", "SpotifyAdapterError", "SpotifyAuthManager",
    "SpotifyErrorKind", "SpotifyReadOnlyAdapter", "CredentialEnvelope",
    "CredentialStore", "TokenProvider", "WindowsCredentialStore", "mask_identifier",
    "resolve_client_id", "MANAGED_DESCRIPTION", "PROVISIONING_SCOPE",
    "SpotifyManagedPlaylistProvisioner",
    "SpotifyManagedPlaylistPolicyMismatch",
]
