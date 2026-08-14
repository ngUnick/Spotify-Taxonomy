"""Application-specific, integrity-checked SQLite backup and restore."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from spotify_playlist_taxonomy import __version__
from spotify_playlist_taxonomy.runtime import RuntimePaths

from .database import assert_integrity, connect_database, current_schema_version


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_format_version: str
    created_at: str
    application_version: str
    schema_version: int
    database_filename: str
    sha256: str
    taxonomy_version: str | None
    specification_versions: tuple[str, ...]


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _manifest_path(backup_path: Path) -> Path:
    return backup_path.with_suffix(".json")


def _require_managed_backup_path(paths: RuntimePaths, backup_path: Path) -> Path:
    resolved = backup_path.resolve()
    if resolved.parent != paths.backup_dir.resolve() or not resolved.name.startswith("backup-"):
        raise BackupError("backup must be an application backup inside the runtime backup directory")
    return resolved


def create_backup(paths: RuntimePaths, *, retention_count: int) -> Path:
    if not paths.database_path.is_file():
        raise BackupError("database does not exist")
    paths.ensure_backup_dir()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = paths.backup_dir / f"backup-{timestamp}.sqlite3"

    source = connect_database(paths.database_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.row_factory = sqlite3.Row
        assert_integrity(destination)
        schema_version = current_schema_version(destination)
        taxonomy_row = destination.execute(
            "SELECT taxonomy_version FROM taxonomy_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        specification_versions = tuple(
            row[0] for row in destination.execute(
                "SELECT specification_version FROM specification_versions ORDER BY specification_version"
            )
        )
    finally:
        destination.close()
        source.close()

    manifest = BackupManifest(
        "1",
        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        __version__,
        schema_version,
        paths.database_filename,
        _digest(backup_path),
        taxonomy_row[0] if taxonomy_row else None,
        specification_versions,
    )
    _manifest_path(backup_path).write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True), encoding="utf-8"
    )
    prune_backups(paths, retention_count=retention_count)
    return backup_path


def load_and_verify_backup(paths: RuntimePaths, backup_path: Path) -> BackupManifest:
    backup_path = _require_managed_backup_path(paths, backup_path)
    manifest_path = _manifest_path(backup_path)
    if not backup_path.is_file() or not manifest_path.is_file():
        raise BackupError("backup or manifest is missing")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BackupManifest(
            backup_format_version=payload["backup_format_version"],
            created_at=payload["created_at"],
            application_version=payload["application_version"],
            schema_version=int(payload["schema_version"]),
            database_filename=payload["database_filename"],
            sha256=payload["sha256"],
            taxonomy_version=payload.get("taxonomy_version"),
            specification_versions=tuple(payload.get("specification_versions", [])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupError("backup manifest is invalid") from error
    if manifest.backup_format_version != "1":
        raise BackupError("unsupported backup format")
    if manifest.database_filename != paths.database_filename:
        raise BackupError("backup database filename does not match runtime configuration")
    if _digest(backup_path) != manifest.sha256:
        raise BackupError("backup digest mismatch")
    connection = connect_database(backup_path)
    try:
        assert_integrity(connection)
        actual_version = current_schema_version(connection)
    finally:
        connection.close()
    if actual_version != manifest.schema_version:
        raise BackupError("backup schema does not match its manifest")
    from .database import load_migrations
    supported = len(load_migrations())
    if actual_version > supported:
        raise BackupError("backup schema is newer than this application supports")
    return manifest


def restore_backup(paths: RuntimePaths, backup_path: Path, *, retention_count: int) -> None:
    backup_path = _require_managed_backup_path(paths, backup_path)
    load_and_verify_backup(paths, backup_path)
    paths.ensure_database_parent()
    temporary_handle = tempfile.NamedTemporaryFile(
        prefix="restore-", suffix=".sqlite3", dir=paths.state_dir, delete=False
    )
    temporary_path = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        shutil.copy2(backup_path, temporary_path)
        if paths.database_path.exists():
            create_backup(paths, retention_count=retention_count)
        connection = connect_database(temporary_path)
        try:
            assert_integrity(connection)
        finally:
            connection.close()
        os.replace(temporary_path, paths.database_path)
        restored = connect_database(paths.database_path)
        try:
            assert_integrity(restored)
        finally:
            restored.close()
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def prune_backups(paths: RuntimePaths, *, retention_count: int) -> None:
    if retention_count < 1 or not paths.backup_dir.exists():
        return
    backups: list[Path] = []
    for candidate in paths.backup_dir.glob("backup-*.sqlite3"):
        manifest_path = _manifest_path(candidate)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            payload.get("backup_format_version") == "1"
            and payload.get("database_filename") == paths.database_filename
            and isinstance(payload.get("sha256"), str)
        ):
            backups.append(candidate)
    backups.sort(key=lambda item: item.name, reverse=True)
    for backup in backups[retention_count:]:
        managed = _require_managed_backup_path(paths, backup)
        managed.unlink(missing_ok=True)
        _manifest_path(managed).unlink(missing_ok=True)
