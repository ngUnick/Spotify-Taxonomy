"""SQLite connections, migrations, integrity checks, and taxonomy synchronization."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from spotify_playlist_taxonomy.config import TaxonomyDocument, taxonomy_digest


class PersistenceError(RuntimeError):
    pass


class MigrationError(PersistenceError):
    pass


class TaxonomySyncError(PersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True, slots=True)
class TaxonomyState:
    configured_version: str
    configured_digest: str
    persisted_version: str | None
    persisted_digest: str | None
    configured_playlist_count: int
    persisted_playlist_count: int
    in_sync: bool


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise PersistenceError("SQLite foreign-key enforcement could not be enabled")
    return connection


def load_migrations() -> tuple[Migration, ...]:
    migration_root = files("spotify_playlist_taxonomy.persistence.migrations")
    migrations: list[Migration] = []
    for resource in sorted(migration_root.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".sql"):
            continue
        prefix, separator, _ = resource.name.partition("_")
        if not separator or not prefix.isdigit():
            raise MigrationError(f"invalid migration filename: {resource.name}")
        sql = resource.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                int(prefix),
                resource.name,
                sql,
                hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    versions = [item.version for item in migrations]
    expected = list(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationError(f"migration versions must be contiguous: {versions!r}")
    return tuple(migrations)


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def apply_migrations(connection: sqlite3.Connection) -> int:
    migrations = load_migrations()
    _ensure_migration_table(connection)
    applied = {
        row["version"]: (row["name"], row["checksum"])
        for row in connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
    }
    supported = migrations[-1].version if migrations else 0
    if applied and max(applied) > supported:
        raise MigrationError("database schema is newer than this application supports")

    for migration in migrations:
        if migration.version in applied:
            if applied[migration.version] != (migration.name, migration.checksum):
                raise MigrationError(f"migration {migration.version} checksum/name mismatch")
            continue
        escaped_name = migration.name.replace("'", "''")
        escaped_checksum = migration.checksum.replace("'", "''")
        escaped_time = utc_now().replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
            f"VALUES ({migration.version}, '{escaped_name}', '{escaped_checksum}', "
            f"'{escaped_time}');\nCOMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise MigrationError(f"migration {migration.name} failed: {error}") from error

    assert_integrity(connection)
    return supported


def current_schema_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()
    return int(row["version"])


def assert_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise PersistenceError(f"SQLite integrity check failed: {result}")


def _latest_taxonomy(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT taxonomy_version, digest FROM taxonomy_versions ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _revision_rows(connection: sqlite3.Connection, version: str) -> dict[str, sqlite3.Row]:
    return {
        row["logical_id"]: row
        for row in connection.execute(
            """
            SELECT r.logical_id, p.dimension, r.display_name, r.enabled, r.specification_status
            FROM logical_playlist_revisions AS r
            JOIN logical_playlists AS p USING (logical_id)
            WHERE r.taxonomy_version = ?
            """,
            (version,),
        )
    }


def sync_taxonomy(connection: sqlite3.Connection, document: TaxonomyDocument) -> bool:
    digest = taxonomy_digest(document)
    latest = _latest_taxonomy(connection)
    entries = {item.logical_id: item for item in document.playlists}

    if latest is not None and latest["taxonomy_version"] == document.taxonomy_version:
        if latest["digest"] != digest:
            raise TaxonomySyncError("taxonomy content changed without a taxonomy_version change")
        persisted = _revision_rows(connection, document.taxonomy_version)
        expected = {
            key: (value.dimension.value, value.display_name, int(value.enabled), value.specification_status.value)
            for key, value in entries.items()
        }
        actual = {
            key: (row["dimension"], row["display_name"], row["enabled"], row["specification_status"])
            for key, row in persisted.items()
        }
        if actual != expected:
            raise TaxonomySyncError("persisted taxonomy rows do not match their recorded digest")
        return False

    existing_dimensions = {
        row["logical_id"]: row["dimension"]
        for row in connection.execute("SELECT logical_id, dimension FROM logical_playlists")
    }
    missing = set(existing_dimensions) - set(entries)
    if missing:
        raise TaxonomySyncError(
            "taxonomy update omits existing logical IDs: " + ", ".join(sorted(missing))
        )
    changed_dimensions = [
        logical_id
        for logical_id, dimension in existing_dimensions.items()
        if entries[logical_id].dimension.value != dimension
    ]
    if changed_dimensions:
        raise TaxonomySyncError(
            "taxonomy update changes dimensions for: " + ", ".join(sorted(changed_dimensions))
        )

    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO taxonomy_versions(taxonomy_version, schema_version, digest, persisted_at) "
            "VALUES (?, ?, ?, ?)",
            (document.taxonomy_version, document.schema_version, digest, utc_now()),
        )
        for entry in document.playlists:
            connection.execute(
                "INSERT OR IGNORE INTO logical_playlists(logical_id, dimension, created_at) VALUES (?, ?, ?)",
                (entry.logical_id, entry.dimension.value, utc_now()),
            )
            connection.execute(
                """
                INSERT INTO logical_playlist_revisions(
                    taxonomy_version, logical_id, display_name, enabled, specification_status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document.taxonomy_version, entry.logical_id, entry.display_name,
                    int(entry.enabled), entry.specification_status.value,
                ),
            )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise TaxonomySyncError(f"taxonomy synchronization failed: {error}") from error
    return True


def taxonomy_state(connection: sqlite3.Connection, document: TaxonomyDocument) -> TaxonomyState:
    digest = taxonomy_digest(document)
    latest = _latest_taxonomy(connection)
    if latest is None:
        return TaxonomyState(document.taxonomy_version, digest, None, None, len(document.playlists), 0, False)
    persisted = _revision_rows(connection, latest["taxonomy_version"])
    expected = {
        item.logical_id: (
            item.dimension.value,
            item.display_name,
            int(item.enabled),
            item.specification_status.value,
        )
        for item in document.playlists
    }
    actual = {
        logical_id: (
            row["dimension"],
            row["display_name"],
            row["enabled"],
            row["specification_status"],
        )
        for logical_id, row in persisted.items()
    }
    count = len(persisted)
    return TaxonomyState(
        document.taxonomy_version, digest,
        latest["taxonomy_version"], latest["digest"],
        len(document.playlists), int(count),
        latest["taxonomy_version"] == document.taxonomy_version
        and latest["digest"] == digest
        and actual == expected,
    )
