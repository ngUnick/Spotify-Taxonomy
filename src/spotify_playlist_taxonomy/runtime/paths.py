"""Resolve and lazily create the per-user runtime layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class RuntimePathError(ValueError):
    pass


def discover_repository_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    database_filename: str = "app.db"

    @classmethod
    def resolve(
        cls,
        *,
        cli_data_dir: Path | None = None,
        environment: Mapping[str, str] | None = None,
        database_filename: str = "app.db",
        repository_root: Path | None = None,
    ) -> "RuntimePaths":
        env = os.environ if environment is None else environment
        if cli_data_dir is not None:
            root = cli_data_dir
        elif env.get("SPOTIFY_TAXONOMY_DATA_DIR"):
            root = Path(env["SPOTIFY_TAXONOMY_DATA_DIR"])
        elif env.get("LOCALAPPDATA"):
            root = Path(env["LOCALAPPDATA"]) / "SpotifyPlaylistTaxonomy"
        else:
            raise RuntimePathError("LOCALAPPDATA is required when no runtime override is set")

        resolved = root.expanduser().resolve()
        if repository_root is not None and resolved.is_relative_to(repository_root.resolve()):
            raise RuntimePathError("runtime data must remain outside the repository")
        return cls(resolved, database_filename)

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def database_path(self) -> Path:
        return self.state_dir / self.database_filename

    @property
    def backup_dir(self) -> Path:
        return self.root / "backups"

    @property
    def outbound_batch_dir(self) -> Path:
        return self.root / "batches" / "outbound"

    @property
    def inbound_batch_dir(self) -> Path:
        return self.root / "batches" / "inbound"

    @property
    def export_dir(self) -> Path:
        return self.root / "exports"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    def ensure_database_parent(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def ensure_backup_dir(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def ensure_log_dir(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
