"""SQLite persistence and backup facilities."""

from .backup import BackupError, BackupManifest, create_backup, load_and_verify_backup, restore_backup
from .database import (
    MigrationError,
    PersistenceError,
    TaxonomyState,
    TaxonomySyncError,
    apply_migrations,
    assert_integrity,
    connect_database,
    current_schema_version,
    sync_taxonomy,
    taxonomy_state,
)
from .store import RecordConflictError, RecordNotFoundError, SQLiteStateStore
from .phase3 import AccountMismatchError, Phase3StateError, Phase3Store
from .research import CacheRecord, ResearchStore, ResearchSubject

__all__ = [
    "BackupError", "BackupManifest", "MigrationError", "PersistenceError",
    "RecordConflictError", "RecordNotFoundError", "create_backup",
    "load_and_verify_backup", "restore_backup",
    "SQLiteStateStore", "TaxonomyState", "TaxonomySyncError", "apply_migrations",
    "assert_integrity", "connect_database", "current_schema_version",
    "sync_taxonomy", "taxonomy_state",
    "AccountMismatchError", "Phase3StateError", "Phase3Store",
    "CacheRecord", "ResearchStore", "ResearchSubject",
]
