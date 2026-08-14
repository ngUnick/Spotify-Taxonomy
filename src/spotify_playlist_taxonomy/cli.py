"""Foundational local-only command-line interface."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
from pydantic import ValidationError

from spotify_playlist_taxonomy import __version__
from spotify_playlist_taxonomy.config import (
    ApplicationConfig,
    TaxonomyDocument,
    load_application_config,
    load_specification_document,
    load_taxonomy_document,
    resolve_taxonomy_path,
    taxonomy_digest,
)
from spotify_playlist_taxonomy.persistence import (
    BackupError,
    PersistenceError,
    apply_migrations,
    assert_integrity,
    connect_database,
    create_backup,
    current_schema_version,
    load_and_verify_backup,
    restore_backup,
    sync_taxonomy,
    taxonomy_state,
)
from spotify_playlist_taxonomy.runtime import RuntimePaths, discover_repository_root

DEFAULT_CONFIG_PATH = Path("config/application.yaml")

app = typer.Typer(no_args_is_help=True, help="Local Spotify Playlist Taxonomy foundations.")
config_app = typer.Typer(no_args_is_help=True, help="Validate source-controlled configuration.")
runtime_app = typer.Typer(no_args_is_help=True, help="Inspect local runtime paths.")
database_app = typer.Typer(no_args_is_help=True, help="Initialize and inspect SQLite state.")
backup_app = typer.Typer(no_args_is_help=True, help="Create, verify, and restore SQLite backups.")
app.add_typer(config_app, name="config")
app.add_typer(runtime_app, name="runtime")
app.add_typer(database_app, name="db")
app.add_typer(backup_app, name="backup")


def _load_documents(config_path: Path) -> tuple[ApplicationConfig, TaxonomyDocument, Path]:
    config_path = config_path.resolve()
    config = load_application_config(config_path)
    taxonomy_path = resolve_taxonomy_path(config_path, config)
    taxonomy = load_taxonomy_document(taxonomy_path)
    return config, taxonomy, taxonomy_path


def _paths(config: ApplicationConfig, data_dir: Path | None) -> RuntimePaths:
    return RuntimePaths.resolve(
        cli_data_dir=data_dir,
        database_filename=config.database_filename,
        repository_root=discover_repository_root(Path.cwd()),
    )


def _configure_file_logging(config: ApplicationConfig, paths: RuntimePaths) -> None:
    if not config.file_logging:
        return
    paths.ensure_log_dir()
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        filename=paths.log_dir / "spotify-taxonomy.log",
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _fail(error: Exception) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


@app.callback()
def root(
    version: bool = typer.Option(False, "--version", help="Show the application version."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@config_app.command("validate")
def validate_config(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    specification: list[Path] | None = typer.Option(None, "--specification"),
) -> None:
    """Validate configuration without creating runtime state."""
    try:
        _, taxonomy, taxonomy_path = _load_documents(config_path)
        logical_ids = {item.logical_id for item in taxonomy.playlists}
        for path in specification or []:
            document = load_specification_document(path)
            if document.logical_playlist_id not in logical_ids:
                raise ValueError(f"{path} references an unknown logical playlist")
        typer.echo(
            f"Valid: taxonomy={taxonomy.taxonomy_version} playlists={len(taxonomy.playlists)} "
            f"digest={taxonomy_digest(taxonomy)} source={taxonomy_path}"
        )
    except (OSError, ValueError, ValidationError) as error:
        _fail(error)


@runtime_app.command("paths")
def show_runtime_paths(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show resolved paths without creating them."""
    try:
        config, _, _ = _load_documents(config_path)
        paths = _paths(config, data_dir)
        for name, path in (
            ("root", paths.root), ("database", paths.database_path),
            ("backups", paths.backup_dir), ("batch_outbound", paths.outbound_batch_dir),
            ("batch_inbound", paths.inbound_batch_dir), ("exports", paths.export_dir),
            ("logs", paths.log_dir),
        ):
            typer.echo(f"{name}: {path} exists={path.exists()}")
    except (OSError, ValueError, ValidationError) as error:
        _fail(error)


def _database_status(config_path: Path, data_dir: Path | None) -> tuple[RuntimePaths, int, object]:
    config, taxonomy, _ = _load_documents(config_path)
    paths = _paths(config, data_dir)
    if not paths.database_path.is_file():
        raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        version = current_schema_version(connection)
        state = taxonomy_state(connection, taxonomy)
    finally:
        connection.close()
    return paths, version, state


@database_app.command("init")
def initialize_database(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Validate, migrate, and transactionally synchronize taxonomy state."""
    try:
        config, taxonomy, _ = _load_documents(config_path)
        paths = _paths(config, data_dir)
        paths.ensure_database_parent()
        _configure_file_logging(config, paths)
        connection = connect_database(paths.database_path)
        try:
            schema_version = apply_migrations(connection)
            changed = sync_taxonomy(connection, taxonomy)
        finally:
            connection.close()
        typer.echo(
            f"Initialized: database={paths.database_path} schema={schema_version} "
            f"taxonomy={taxonomy.taxonomy_version} synchronized={changed}"
        )
    except (OSError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@database_app.command("status")
def database_status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    try:
        paths, version, state = _database_status(config_path, data_dir)
        typer.echo(f"database: {paths.database_path}")
        typer.echo(f"schema_version: {version}")
        typer.echo(f"configured_taxonomy: {state.configured_version} {state.configured_digest}")
        typer.echo(f"persisted_taxonomy: {state.persisted_version} {state.persisted_digest}")
        typer.echo(
            f"playlists: configured={state.configured_playlist_count} persisted={state.persisted_playlist_count}"
        )
        typer.echo(f"taxonomy_in_sync: {state.in_sync}")
    except (OSError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@app.command("status")
def overall_status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    database_status(config_path, data_dir)


@backup_app.command("create")
def backup_create(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    try:
        config, _, _ = _load_documents(config_path)
        paths = _paths(config, data_dir)
        backup_path = create_backup(paths, retention_count=config.backup_retention_count)
        typer.echo(f"Created: {backup_path}")
    except (OSError, ValueError, ValidationError, PersistenceError, BackupError) as error:
        _fail(error)


@backup_app.command("verify")
def backup_verify(
    backup_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    try:
        config, _, _ = _load_documents(config_path)
        paths = _paths(config, data_dir)
        manifest = load_and_verify_backup(paths, backup_path)
        typer.echo(f"Valid: schema={manifest.schema_version} digest={manifest.sha256}")
    except (OSError, ValueError, ValidationError, PersistenceError, BackupError) as error:
        _fail(error)


@backup_app.command("restore")
def backup_restore(
    backup_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
    yes: bool = typer.Option(False, "--yes", help="Confirm replacement of the runtime database."),
) -> None:
    try:
        config, _, _ = _load_documents(config_path)
        paths = _paths(config, data_dir)
        load_and_verify_backup(paths, backup_path)
        if not yes and not typer.confirm(f"Restore {backup_path.name} to {paths.database_path}?"):
            raise typer.Abort()
        restore_backup(paths, backup_path, retention_count=config.backup_retention_count)
        typer.echo(f"Restored: {paths.database_path}")
    except (OSError, ValueError, ValidationError, PersistenceError, BackupError) as error:
        _fail(error)
