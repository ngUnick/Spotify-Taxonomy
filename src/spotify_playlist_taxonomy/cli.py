"""Foundational local-only command-line interface."""

from __future__ import annotations

import logging
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, TextIO

import httpx
import typer
from pydantic import ValidationError

from spotify_playlist_taxonomy import __version__
from spotify_playlist_taxonomy.adapters.spotify import (
    SpotifyAdapterError,
    SpotifyAuthManager,
    SpotifyReadOnlyAdapter,
    WindowsCredentialStore,
    mask_identifier,
    resolve_client_id,
)
from spotify_playlist_taxonomy.config import (
    ApplicationConfig,
    Phase4ValidationError,
    TaxonomyDocument,
    load_application_config,
    load_research_policy_document,
    load_phase4_bundle,
    load_taxonomy_document,
    resolve_taxonomy_path,
    resolve_research_policy_path,
    taxonomy_digest,
)
from spotify_playlist_taxonomy.domain.ingestion import InboxKind, MatchCandidateState
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, PlaylistRole
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
    ResearchStore,
)
from spotify_playlist_taxonomy.adapters.research import (
    MusicBrainzResearchProvider,
    WikidataResearchProvider,
)
from spotify_playlist_taxonomy.domain.research import SufficiencyState
from spotify_playlist_taxonomy.persistence.phase3 import Phase3StateError, Phase3Store
from spotify_playlist_taxonomy.runtime import RuntimePaths, discover_repository_root
from spotify_playlist_taxonomy.workflows.phase3 import Phase3Coordinator
from spotify_playlist_taxonomy.workflows.review import Phase3ReviewService
from spotify_playlist_taxonomy.workflows.research import (
    EvidenceSufficiencyService,
    ResearchCoordinator,
)
from spotify_playlist_taxonomy.workflows.phase6 import Phase6Service
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service

DEFAULT_CONFIG_PATH = Path("config/application.yaml")

app = typer.Typer(no_args_is_help=True, help="Local Spotify Playlist Taxonomy foundations.")
config_app = typer.Typer(no_args_is_help=True, help="Validate source-controlled configuration.")
runtime_app = typer.Typer(no_args_is_help=True, help="Inspect local runtime paths.")
database_app = typer.Typer(no_args_is_help=True, help="Initialize and inspect SQLite state.")
backup_app = typer.Typer(no_args_is_help=True, help="Create, verify, and restore SQLite backups.")
spotify_app = typer.Typer(no_args_is_help=True, help="Spotify authorization and read-only checks.")
spotify_auth_app = typer.Typer(no_args_is_help=True, help="Manage protected Spotify authorization.")
spotify_account_app = typer.Typer(no_args_is_help=True, help="Bind the authenticated Spotify account locally.")
spotify_source_app = typer.Typer(no_args_is_help=True, help="Register read-only SOURCE and INBOX inputs.")
spotify_ingest_app = typer.Typer(no_args_is_help=True, help="Run and inspect read-only source scans.")
catalogue_app = typer.Typer(no_args_is_help=True, help="Review the local canonical catalogue.")
catalogue_identity_app = typer.Typer(no_args_is_help=True, help="Inspect account-scoped track identities.")
catalogue_match_app = typer.Typer(no_args_is_help=True, help="Review canonical matching candidates.")
catalogue_link_app = typer.Typer(no_args_is_help=True, help="Manage explicit canonical identity links.")
research_app = typer.Typer(no_args_is_help=True, help="Gather and inspect independent recording evidence.")
derive_app = typer.Typer(no_args_is_help=True, help="Derive offline characteristics from persisted evidence.")
classify_app = typer.Typer(no_args_is_help=True, help="Evaluate offline semantic proposals from characteristics.")
review_app = typer.Typer(no_args_is_help=True, help="Review local semantic proposals without network access.")
calibration_app = typer.Typer(no_args_is_help=True, help="Report observational local calibration.")
app.add_typer(config_app, name="config")
app.add_typer(runtime_app, name="runtime")
app.add_typer(database_app, name="db")
app.add_typer(backup_app, name="backup")
app.add_typer(spotify_app, name="spotify")
spotify_app.add_typer(spotify_auth_app, name="auth")
spotify_app.add_typer(spotify_account_app, name="account")
spotify_app.add_typer(spotify_source_app, name="source")
spotify_app.add_typer(spotify_ingest_app, name="ingest")
app.add_typer(catalogue_app, name="catalogue")
catalogue_app.add_typer(catalogue_identity_app, name="identity")
catalogue_app.add_typer(catalogue_match_app, name="match")
catalogue_app.add_typer(catalogue_link_app, name="link")
app.add_typer(research_app, name="research")
app.add_typer(derive_app, name="derive")
app.add_typer(classify_app, name="classify")
app.add_typer(review_app, name="review")
app.add_typer(calibration_app, name="calibration")


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
    # HTTP transport logs can contain resource URLs or authorization headers.
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True


def _fail(error: Exception) -> None:
    _safe_echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def _render_cli_text(message: object, encoding: str | None) -> str:
    """Render safely without mutating the underlying Unicode source text."""

    text = str(message)
    selected_encoding = encoding or "utf-8"
    try:
        text.encode(selected_encoding, errors="strict")
    except LookupError:
        selected_encoding = "utf-8"
    except UnicodeEncodeError:
        return text.encode(
            selected_encoding, errors="backslashreplace"
        ).decode(selected_encoding, errors="strict")
    return text


def _safe_echo(
    message: object = "", *, err: bool = False, file: TextIO | None = None
) -> None:
    """Write deliberate CLI text to UTF-capable or restricted output streams."""

    stream = file if file is not None else (sys.stderr if err else sys.stdout)
    typer.echo(_render_cli_text(message, getattr(stream, "encoding", None)), file=stream)


def _spotify_http_client(config: ApplicationConfig) -> httpx.Client:
    return httpx.Client(
        timeout=config.spotify.request_timeout_seconds,
        verify=True,
        follow_redirects=False,
        trust_env=False,
    )


def _spotify_auth_manager(
    config: ApplicationConfig,
    client_id: str,
    http_client: httpx.Client,
    *,
    callback_port: int | None = None,
    authorization_url_sink: Callable[[str], None] | None = None,
) -> SpotifyAuthManager:
    sink = authorization_url_sink or (lambda value: None)
    return SpotifyAuthManager(
        client_id=client_id,
        credential_store=WindowsCredentialStore(),
        http_client=http_client,
        callback_timeout_seconds=config.spotify.callback_timeout_seconds,
        callback_port=config.spotify.callback_port if callback_port is None else callback_port,
        authorization_url_sink=sink,
        warning_sink=lambda message: _safe_echo(f"Warning: {message}", err=True),
    )


@contextmanager
def _phase3_store(
    config_path: Path, data_dir: Path | None
) -> Iterator[tuple[ApplicationConfig, RuntimePaths, Phase3Store]]:
    config, _, _ = _load_documents(config_path)
    paths = _paths(config, data_dir)
    if not paths.database_path.is_file():
        raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        yield config, paths, Phase3Store(connection)
    finally:
        connection.close()


@contextmanager
def _phase3_online(
    config_path: Path,
    data_dir: Path | None,
    client_id: str | None,
) -> Iterator[Phase3Coordinator]:
    with _phase3_store(config_path, data_dir) as (config, paths, store):
        _configure_file_logging(config, paths)
        resolved = resolve_client_id(client_id)
        with _spotify_http_client(config) as http_client:
            manager = _spotify_auth_manager(config, resolved, http_client)
            reader = SpotifyReadOnlyAdapter(
                token_provider=manager,
                http_client=http_client,
                max_read_attempts=config.spotify.max_read_attempts,
            )
            yield Phase3Coordinator(reader=reader, store=store)


def _phase3_review(store: Phase3Store) -> Phase3ReviewService:
    binding = store.active_account()
    if binding is None:
        raise Phase3StateError("an active Spotify account binding is required")
    return Phase3ReviewService(store=store, binding_id=binding.binding_id)


@contextmanager
def _research_store(
    config_path: Path, data_dir: Path | None,
) -> Iterator[tuple[ApplicationConfig, RuntimePaths, ResearchStore]]:
    config, _, _ = _load_documents(config_path)
    paths = _paths(config, data_dir)
    if not paths.database_path.is_file():
        raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 5:
            raise PersistenceError("database requires migration 5; run 'db init'")
        yield config, paths, ResearchStore(connection)
    finally:
        connection.close()


@contextmanager
def _phase6_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase6Service]:
    config_path = config_path.resolve()
    config, _, _ = _load_documents(config_path)
    paths = _paths(config, data_dir)
    if not paths.database_path.is_file():
        raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 6:
            raise PersistenceError("database requires migration 6; run 'db init'")
        yield Phase6Service(connection, load_phase4_bundle(config_path))
    finally:
        connection.close()


@contextmanager
def _phase7_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase7Service]:
    config_path = config_path.resolve()
    config, _, _ = _load_documents(config_path)
    paths = _paths(config, data_dir)
    if not paths.database_path.is_file(): raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 7: raise PersistenceError("database requires migration 7; run 'db init'")
        yield Phase7Service(connection, load_phase4_bundle(config_path))
    finally:
        connection.close()


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
) -> None:
    """Validate the complete source-controlled taxonomy bundle without runtime state."""
    try:
        bundle = load_phase4_bundle(config_path)
        config = load_application_config(config_path.resolve())
        research_policy = load_research_policy_document(
            resolve_research_policy_path(config_path.resolve(), config)
        )
        taxonomy = bundle.taxonomy
        typer.echo(
            f"Valid: taxonomy={taxonomy.taxonomy_version} playlists={len(taxonomy.playlists)} "
            f"characteristics={len(bundle.vocabulary.characteristics)} "
            f"specifications={len(bundle.specifications)} "
            f"output_targets={len(bundle.output_targets.targets)} "
            f"routing={bundle.genre_variant_routing.routing_policy_version} "
            f"status={bundle.manifest.status.value} "
            f"research_policy={research_policy.policy_version} "
            f"research_providers={len([item for item in research_policy.providers if item.enabled])} "
            f"digest={bundle.digest} source={bundle.taxonomy_path}"
        )
    except (OSError, ValueError, ValidationError, Phase4ValidationError) as error:
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


@spotify_auth_app.command("status")
def spotify_auth_status(
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
) -> None:
    """Inspect protected local authorization without contacting Spotify."""
    try:
        config, _, _ = _load_documents(config_path)
        resolved = resolve_client_id(client_id)
        with _spotify_http_client(config) as http_client:
            manager = _spotify_auth_manager(config, resolved, http_client)
            status = manager.status()
        typer.echo(f"client_id: {mask_identifier(resolved)}")
        typer.echo(f"authorized: {status.authorized}")
        if status.authorized:
            typer.echo(f"scopes: {' '.join(status.scopes)}")
            typer.echo(f"authorized_at: {status.authorized_at.isoformat()}")
    except (OSError, ValueError, ValidationError, SpotifyAdapterError) as error:
        _fail(error)


@spotify_auth_app.command("login")
def spotify_auth_login(
    client_id: str | None = typer.Option(None, "--client-id"),
    callback_port: int | None = typer.Option(
        None,
        "--callback-port",
        min=1024,
        max=65535,
        help="Fixed loopback port registered in the Spotify Developer Dashboard.",
    ),
    no_browser: bool = typer.Option(False, "--no-browser"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
) -> None:
    """Authorize with PKCE; the full URL is printed only with --no-browser."""
    try:
        config, _, _ = _load_documents(config_path)
        resolved = resolve_client_id(client_id)
        with _spotify_http_client(config) as http_client:
            manager = _spotify_auth_manager(
                config,
                resolved,
                http_client,
                callback_port=callback_port,
                authorization_url_sink=typer.echo,
            )
            token = manager.login(no_browser=no_browser)
        typer.echo(f"Authorized: client_id={mask_identifier(resolved)}")
        typer.echo(f"scopes: {' '.join(token.scopes)}")
    except (OSError, ValueError, ValidationError, SpotifyAdapterError) as error:
        _fail(error)


@spotify_auth_app.command("logout")
def spotify_auth_logout(
    client_id: str | None = typer.Option(None, "--client-id"),
    yes: bool = typer.Option(False, "--yes", help="Confirm local credential deletion."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
) -> None:
    """Delete the protected local refresh credential; no Spotify call is made."""
    try:
        config, _, _ = _load_documents(config_path)
        resolved = resolve_client_id(client_id)
        if not yes and not typer.confirm(
            f"Delete local Spotify authorization for {mask_identifier(resolved)}?"
        ):
            raise typer.Abort()
        with _spotify_http_client(config) as http_client:
            manager = _spotify_auth_manager(config, resolved, http_client)
            removed = manager.logout()
        typer.echo(f"Local credential removed: {removed}")
        typer.echo("To revoke server-side access, remove the app in your Spotify account settings.")
    except (OSError, ValueError, ValidationError, SpotifyAdapterError) as error:
        _fail(error)


@spotify_app.command("check")
def spotify_check(
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
) -> None:
    """Perform the separately approved minimal GET /me connectivity check."""
    try:
        config, _, _ = _load_documents(config_path)
        resolved = resolve_client_id(client_id)
        with _spotify_http_client(config) as http_client:
            manager = _spotify_auth_manager(config, resolved, http_client)
            reader = SpotifyReadOnlyAdapter(
                token_provider=manager,
                http_client=http_client,
                max_read_attempts=config.spotify.max_read_attempts,
            )
            account = reader.read_current_account()
            status = manager.status()
        typer.echo(f"account_id: {mask_identifier(account.account_id)}")
        typer.echo(f"spotify_user_id: {mask_identifier(account.spotify_user_id)}")
        typer.echo(f"scopes: {' '.join(status.scopes)}")
    except (OSError, ValueError, ValidationError, SpotifyAdapterError) as error:
        _fail(error)


@spotify_account_app.command("status")
def spotify_account_status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show the local account binding without contacting Spotify."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            binding = store.active_account()
        if binding is None:
            typer.echo("bound: false")
            return
        typer.echo("bound: true")
        typer.echo(f"account_id: {mask_identifier(binding.account_id)}")
        typer.echo(f"spotify_user_id: {mask_identifier(binding.current_user_id)}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@spotify_account_app.command("bind")
def spotify_account_bind(
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Bind the current authenticated account using only GET /me."""
    try:
        with _phase3_online(config_path, data_dir, client_id) as coordinator:
            account = coordinator.bind_account()
        typer.echo(f"Bound Spotify account: {mask_identifier(account.account_id)}")
    except (
        OSError, ValueError, ValidationError, PersistenceError,
        Phase3StateError, SpotifyAdapterError,
    ) as error:
        _fail(error)


@spotify_account_app.command("unbind")
def spotify_account_unbind(
    yes: bool = typer.Option(False, "--yes", help="Confirm local account unbinding."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Unbind locally after all ingestion sources have been disabled."""
    try:
        if not yes and not typer.confirm("Unbind the local Spotify account?"):
            raise typer.Abort()
        with _phase3_store(config_path, data_dir) as (_, _, store):
            store.unbind_account()
        typer.echo("Local Spotify account binding removed.")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@spotify_source_app.command("discover")
def spotify_source_discover(
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Ephemerally list current-user playlists; discovery is not persisted."""
    try:
        with _phase3_online(config_path, data_dir, client_id) as coordinator:
            discovered = coordinator.discover_playlists()
        for item, ownership in discovered:
            _safe_echo(
                f"{item.playlist_id}\t{ownership.value}\t"
                f"collaborative={item.collaborative}\t{item.name}"
            )
        _safe_echo(f"Discovered: {len(discovered)} (not persisted)")
    except (
        OSError, ValueError, ValidationError, PersistenceError,
        Phase3StateError, SpotifyAdapterError,
    ) as error:
        _fail(error)


@spotify_source_app.command("register-playlist")
def spotify_source_register_playlist(
    playlist_id: str = typer.Argument(...),
    role: PlaylistRole = typer.Option(..., "--role", help="source or inbox"),
    shazam: bool = typer.Option(False, "--shazam", help="Register this INBOX as the selected Shazam playlist."),
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly register one accessible playlist as SOURCE or INBOX."""
    try:
        if role is PlaylistRole.MANAGED:
            raise ValueError("Phase 3 can register only SOURCE or INBOX playlists")
        if shazam and role is not PlaylistRole.INBOX:
            raise ValueError("--shazam requires --role inbox")
        with _phase3_online(config_path, data_dir, client_id) as coordinator:
            source_id = coordinator.register_playlist(
                playlist_id,
                role=role,
                inbox_kind=InboxKind.SHAZAM if shazam else None,
            )
        typer.echo(f"Registered source: {source_id}")
    except (
        OSError, ValueError, ValidationError, PersistenceError,
        Phase3StateError, SpotifyAdapterError,
    ) as error:
        _fail(error)


@spotify_source_app.command("register-liked")
def spotify_source_register_liked(
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Register Liked Songs as the account's Saved Tracks INBOX."""
    try:
        with _phase3_online(config_path, data_dir, client_id) as coordinator:
            source_id = coordinator.register_liked()
        typer.echo(f"Registered Liked Songs source: {source_id}")
    except (
        OSError, ValueError, ValidationError, PersistenceError,
        Phase3StateError, SpotifyAdapterError,
    ) as error:
        _fail(error)


@spotify_source_app.command("list")
def spotify_source_list(
    include_disabled: bool = typer.Option(False, "--include-disabled"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """List explicitly registered sources from local state."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            binding = store.active_account()
            sources = () if binding is None else store.list_sources(
                binding.binding_id, active_only=not include_disabled
            )
        for source in sources:
            typer.echo(
                f"{source.source_id}\t{source.kind.value}\t{source.role.value}\t"
                f"active={source.active}\tinbox={source.inbox_kind.value if source.inbox_kind else '-'}"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@spotify_source_app.command("disable")
def spotify_source_disable(
    source_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Deliberately disable a registered source without deleting its history."""
    try:
        if not yes and not typer.confirm(f"Disable source {source_id}?"):
            raise typer.Abort()
        with _phase3_store(config_path, data_dir) as (_, _, store):
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            store.disable_source(source_id, binding.binding_id)
        typer.echo(f"Disabled source: {source_id}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@spotify_ingest_app.command("scan")
def spotify_ingest_scan(
    source_id: str = typer.Argument(...),
    resume: bool = typer.Option(False, "--resume", help="Resume only a snapshot-stable playlist scan."),
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Run a read-only, publish-on-completion source scan."""
    try:
        with _phase3_online(config_path, data_dir, client_id) as coordinator:
            scan_id = coordinator.scan(source_id, resume=resume)
            binding = coordinator.store.active_account()
            assert binding is not None
            status = coordinator.store.scan_row(scan_id, binding.binding_id)["status"]
        typer.echo(f"scan_id: {scan_id}")
        typer.echo(f"status: {status}")
    except (
        OSError, ValueError, ValidationError, PersistenceError,
        Phase3StateError, SpotifyAdapterError,
    ) as error:
        _fail(error)


@spotify_ingest_app.command("status")
def spotify_ingest_status(
    source_id: str | None = typer.Option(None, "--source-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Inspect local scan/publication state without contacting Spotify."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            binding = store.active_account()
            rows = () if binding is None else store.scan_rows(binding.binding_id, source_id)
        for row in rows:
            typer.echo(
                f"{row['scan_id']}\t{row['source_id']}\t{row['status']}\t"
                f"occurrences={row['occurrence_count']}\tissues={row['issue_count']}"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_app.command("status")
def catalogue_status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Summarize published, linked, and reviewable account-scoped catalogue state."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            value = _phase3_review(store).status()
        typer.echo(f"published_identities: {value.published_identity_count}")
        typer.echo(f"linked_identities: {value.linked_identity_count}")
        typer.echo(f"reviewable_candidates: {value.reviewable_candidate_count}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_identity_app.command("show")
def catalogue_identity_show(
    identity_id: str = typer.Argument(...),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show concise identity, version, link, and candidate information for review."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            value = _phase3_review(store).identity(identity_id)
        _safe_echo(f"identity_id: {value.identity.identity_id}")
        _safe_echo(f"title: {value.identity.title}")
        _safe_echo(f"artists: {', '.join(value.identity.artists)}")
        _safe_echo(f"version: {value.identity.version_label or '-'}")
        _safe_echo(
            f"linked_canonical: {value.linked_canonical.canonical_track_id if value.linked_canonical else '-'}"
        )
        _safe_echo(f"candidates: {len(value.candidates)}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("list")
def catalogue_match_list(
    state: MatchCandidateState | None = typer.Option(None, "--state"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """List local matching candidates for explicit review."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            candidates = _phase3_review(store).candidates(state)
        for candidate in candidates:
            typer.echo(
                f"{candidate.candidate_id}\t{candidate.identity_id}\t{candidate.action.value}\t"
                f"{candidate.state.value}\tcanonical={candidate.canonical_track_id or '-'}\t"
                f"basis={','.join(candidate.basis)}"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("show")
def catalogue_match_show(
    candidate_id: str = typer.Argument(...),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show concise evidence for one account-scoped matching candidate."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            value = _phase3_review(store).candidate(candidate_id)
        candidate = value.candidate
        _safe_echo(f"candidate_id: {candidate.candidate_id}")
        _safe_echo(f"state: {candidate.state.value}")
        _safe_echo(f"action: {candidate.action.value}")
        _safe_echo(f"identity: {value.identity.title} — {', '.join(value.identity.artists)}")
        _safe_echo(f"identity_version: {value.identity.version_label or '-'}")
        _safe_echo(
            "canonical: -" if value.canonical is None else
            f"canonical: {value.canonical.display_title} — "
            f"{', '.join(value.canonical.display_artists)}"
        )
        _safe_echo(f"basis: {', '.join(candidate.basis)}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("confirm-existing")
def catalogue_match_confirm_existing(
    candidate_id: str = typer.Argument(...),
    canonical_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly confirm a candidate link to an existing canonical track."""
    try:
        if not yes and not typer.confirm("Confirm this canonical link?"):
            raise typer.Abort()
        with _phase3_store(config_path, data_dir) as (_, _, store):
            _phase3_review(store).confirm_existing(candidate_id, canonical_id)
        typer.echo("Canonical link confirmed.")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("confirm-new")
def catalogue_match_confirm_new(
    candidate_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly confirm creation of a distinct canonical track."""
    try:
        if not yes and not typer.confirm("Create a new canonical track for this identity?"):
            raise typer.Abort()
        with _phase3_store(config_path, data_dir) as (_, _, store):
            canonical_id = _phase3_review(store).confirm_new(candidate_id)
        typer.echo(f"Created canonical track: {canonical_id}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("reject")
def catalogue_match_reject(
    candidate_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Reject a generated canonical match candidate."""
    try:
        if not yes and not typer.confirm("Reject this matching candidate?"):
            raise typer.Abort()
        with _phase3_store(config_path, data_dir) as (_, _, store):
            _phase3_review(store).reject(candidate_id)
        typer.echo("Matching candidate rejected.")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_link_app.command("list")
def catalogue_link_list(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """List active canonical links for the active account binding."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            links = _phase3_review(store).links()
        for value in links:
            _safe_echo(
                f"{value.link_id}\t{value.identity.identity_id}\t"
                f"{value.identity.title} — {', '.join(value.identity.artists)}\t"
                f"canonical={value.canonical.canonical_track_id}"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_link_app.command("unlink")
def catalogue_link_unlink(
    identity_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly unlink an identity; future linking will require confirmation."""
    try:
        if not yes and not typer.confirm("Unlink this platform identity?"):
            raise typer.Abort()
        with _phase3_store(config_path, data_dir) as (_, _, store):
            _phase3_review(store).unlink(identity_id)
        typer.echo("Platform identity unlinked.")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


def _load_research_policy(config_path: Path, config: ApplicationConfig):
    return load_research_policy_document(
        resolve_research_policy_path(config_path.resolve(), config)
    )


@research_app.command("run")
def research_run(
    canonical_ids: list[str] = typer.Argument(..., help="Explicit canonical recording IDs."),
    network: bool = typer.Option(False, "--network", help="Explicitly permit provider network reads."),
    provider_id: str | None = typer.Option(None, "--provider"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass fresh and negative cache entries."),
    limit: int | None = typer.Option(None, "--limit", min=1, max=25),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Research an explicit bounded set; this is the only network-enabled research command."""
    try:
        if not network:
            raise ValueError("research network access requires explicit --network")
        config_path = config_path.resolve()
        bundle = load_phase4_bundle(config_path)
        with _research_store(config_path, data_dir) as (config, _, store):
            policy = _load_research_policy(config_path, config)
            with httpx.Client(
                timeout=policy.request_timeout_seconds,
                verify=True,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                providers = []
                for item in policy.providers:
                    if not item.enabled:
                        continue
                    if item.provider_id == "musicbrainz":
                        providers.append(MusicBrainzResearchProvider(client=http_client, policy=item))
                    elif item.provider_id == "wikidata":
                        providers.append(WikidataResearchProvider(client=http_client, policy=item))
                summary = ResearchCoordinator(
                    store=store, providers=providers, policy=policy,
                    phase4_bundle_digest=bundle.digest,
                ).run(tuple(canonical_ids), provider_id=provider_id, refresh=refresh, limit=limit)
        _safe_echo(
            f"Research complete: jobs={len(summary.job_ids)} provider_calls={summary.provider_calls} "
            f"cache_hits={summary.cache_hits} evidence_created={summary.evidence_created}"
        )
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@research_app.command("show")
def research_show(
    canonical_id: str = typer.Argument(...),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Deliberately inspect normalized local evidence and compact provenance offline."""
    try:
        with _research_store(config_path, data_dir) as (_, _, store):
            store.subject(canonical_id)
            evidence = store.evidence_for_track(canonical_id)
        _safe_echo(f"canonical_track_id: {canonical_id}")
        _safe_echo(f"evidence_count: {len(evidence)}")
        for item in evidence:
            _safe_echo(
                f"{item.evidence_id}\ttype={item.evidence_type or 'legacy'}\t"
                f"provider={item.provider_id or item.source.value}\t"
                f"scope={item.subject_scope or 'legacy'}\tmatch={item.subject_match or 'legacy'}\t"
                f"quality={item.quality_band or 'unknown'}\tstate={item.evidence_state}\t"
                f"value={item.normalized_value}\tsource={item.source_reference or 'none'}"
            )
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@research_app.command("status")
def research_status(
    canonical_id: str | None = typer.Argument(None),
    state: str | None = typer.Option(None, "--state"),
    limit: int = typer.Option(10, "--limit", min=1, max=25),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show provider-independent evidence sufficiency offline."""
    try:
        selected_state = SufficiencyState(state) if state is not None else None
        config_path = config_path.resolve()
        bundle = load_phase4_bundle(config_path)
        with _research_store(config_path, data_dir) as (config, _, store):
            policy = _load_research_policy(config_path, config)
            descriptors = []
            # Descriptors are static and constructing them with a non-networking client performs no I/O.
            with httpx.Client(
                transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                    AssertionError("offline status attempted network access")
                )),
                trust_env=False,
            ) as offline_client:
                for item in policy.providers:
                    if item.provider_id == "musicbrainz" and item.enabled:
                        descriptors.append(MusicBrainzResearchProvider(
                            client=offline_client, policy=item
                        ).descriptor)
                    elif item.provider_id == "wikidata" and item.enabled:
                        descriptors.append(WikidataResearchProvider(
                            client=offline_client, policy=item
                        ).descriptor)
            service = EvidenceSufficiencyService(
                store=store, descriptors=tuple(descriptors),
                phase4_bundle_digest=bundle.digest,
            )
            if canonical_id is not None:
                store.subject(canonical_id)
                canonical_ids = (canonical_id,)
            else:
                canonical_ids = tuple(
                    row[0] for row in store.connection.execute(
                        "SELECT DISTINCT canonical_track_id FROM research_jobs "
                        "ORDER BY canonical_track_id LIMIT ?", (limit,)
                    )
                )
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            reports = tuple(service.assess(value, now=now) for value in canonical_ids)
            attempts = {
                value: store.attempts_for_track(value)
                for value in canonical_ids
            }
        for report in reports:
            visible = tuple(
                item for item in report.assessments
                if selected_state is None or item.state is selected_state
            )
            _safe_echo(
                f"{report.canonical_track_id}\tbundle={report.phase4_bundle_digest}\t"
                f"needs={len(visible)}"
            )
            for item in visible:
                _safe_echo(
                    f"  {item.evidence_type.value}\tstate={item.state.value}\t"
                    f"action={item.next_action.value}\tevidence={len(item.evidence_ids)}\t"
                    f"providers={','.join(item.providers_consulted) or 'none'}\t"
                    f"reasons={','.join(item.reason_codes)}"
                )
            for attempt in attempts[report.canonical_track_id]:
                _safe_echo(
                    f"  provider={attempt['provider_id']}\toutcome={attempt['outcome']}\t"
                    f"reason={attempt['error_category'] or '-'}\tcache_hit={bool(attempt['cache_hit'])}"
                )
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@derive_app.command("run")
def derive_run(
    canonical_ids: list[str] = typer.Argument(..., help="Explicit canonical recording IDs."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Derive only from already-persisted local evidence; never refresh providers."""
    try:
        with _phase6_service(config_path, data_dir) as service:
            total = 0
            for canonical_id in canonical_ids:
                total += len(service.persist_derivations(service.derive(canonical_id)))
        _safe_echo(f"Derived: canonical_tracks={len(canonical_ids)} characteristics={total}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@derive_app.command("show")
def derive_show(
    canonical_id: str = typer.Argument(...),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show compact local characteristic provenance without contacting providers."""
    try:
        with _phase6_service(config_path, data_dir) as service:
            items = service.characteristics(canonical_id)
        _safe_echo(f"canonical_track_id: {canonical_id}")
        for item in items:
            _safe_echo(f"{item.name}\tstate={item.state}\tvalue={item.value}\trule={item.derivation_version}\tevidence={','.join(item.evidence_ids)}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@classify_app.command("run")
def classify_run(
    canonical_ids: list[str] = typer.Argument(..., help="Explicit canonical recording IDs."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Evaluate reviewed semantic specifications from local derived characteristics."""
    try:
        with _phase6_service(config_path, data_dir) as service:
            proposals = sum((len(service.classify(canonical_id)) for canonical_id in canonical_ids), 0)
        _safe_echo(f"Classified: canonical_tracks={len(canonical_ids)} proposals={proposals}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@classify_app.command("show")
def classify_show(
    canonical_id: str = typer.Argument(...),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show persisted explainable proposals; this command makes no decisions."""
    try:
        with _phase6_service(config_path, data_dir) as service:
            rows = service.proposals(canonical_id)
        _safe_echo(f"canonical_track_id: {canonical_id}")
        for row in rows:
            explanation = json.loads(row["explanation_json"])
            _safe_echo(f"{row['logical_playlist_id']}\tdecision={row['decision']}\tstate={row['proposal_state']}\tstrong={','.join(explanation['strong']) or '-'}\tnegative={','.join(explanation['negative']) or '-'}\thard_exclusions={','.join(explanation['hard_exclusions']) or '-'}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


def _render_effective(item: object) -> None:
    value = item
    _safe_echo(f"{value.logical_playlist_id}\tgenerated={value.generated or '-'}\texplicit={value.explicit or '-'}\teffective={value.effective or '-'}\tneeds_review={value.needs_review}\tstale={value.stale}")


@review_app.command("queue")
def review_queue(limit: int = typer.Option(25, "--limit", min=1, max=100), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase7_service(config_path, data_dir) as service: items = service.queue(limit)
        for item in items: _safe_echo(f"{item.canonical_track_id}\t"); _render_effective(item)
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error: _fail(error)


@review_app.command("show")
def review_show(canonical_id: str = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase7_service(config_path, data_dir) as service: items = service.show(canonical_id)
        _safe_echo(f"canonical_track_id: {canonical_id}")
        for item in items: _render_effective(item)
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error: _fail(error)


@review_app.command("decide")
def review_decide(canonical_id: str = typer.Argument(...), semantic_id: str = typer.Argument(...), decision: AssignmentDecision = typer.Argument(...), note: str | None = typer.Option(None, "--note"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase7_service(config_path, data_dir) as service: item = service.decide(canonical_id, semantic_id, decision, note)
        _render_effective(item)
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error: _fail(error)


@review_app.command("clear")
def review_clear(canonical_id: str = typer.Argument(...), semantic_id: str = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase7_service(config_path, data_dir) as service: item = service.clear(canonical_id, semantic_id)
        _render_effective(item)
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error: _fail(error)


@review_app.command("history")
def review_history(canonical_id: str = typer.Argument(...), semantic_id: str | None = typer.Argument(None), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase7_service(config_path, data_dir) as service: rows = service.history(canonical_id, semantic_id)
        for row in rows: _safe_echo(f"{row['logical_playlist_id']}\taction={row['action']}\tdecision={row['decision'] or '-'}\tspecification={row['specification_version']}\tnote={row['note'] or '-'}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error: _fail(error)


@calibration_app.command("show")
def calibration_show(config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase7_service(config_path, data_dir) as service: rows = service.calibration()
        for row in rows: _safe_echo(f"{row['logical_playlist_id']}\treviewed={row['reviewed']}\tagreement={row['agreement']}\tinclude_rejected={row['include_rejected']}\tnoninclude_approved={row['noninclude_approved']}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error: _fail(error)
