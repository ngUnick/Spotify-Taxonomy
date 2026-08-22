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
    SpotifyErrorKind,
    SpotifyAuthManager,
    SpotifyReadOnlyAdapter,
    WindowsCredentialStore,
    mask_identifier,
    resolve_client_id,
    PROVISIONING_SCOPE,
    POPULATION_SCOPES,
    SpotifyManagedPlaylistProvisioner,
    SpotifyManagedTrackAdder,
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
from spotify_playlist_taxonomy.domain.ingestion import (
    InboxKind,
    MatchCandidateAction,
    MatchCandidateState,
)
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
from spotify_playlist_taxonomy.workflows.match_evidence import MatchEvidenceService
from spotify_playlist_taxonomy.workflows.match_evidence_backfill import MatchEvidenceBackfillService
from spotify_playlist_taxonomy.workflows.match_evidence_review import (
    ANOMALY_CATEGORIES,
    MatchEvidenceReviewService,
)
from spotify_playlist_taxonomy.workflows.external_recording_targets import (
    EXTERNAL_RECORDING_CANDIDATE_STRATUM, EXTERNAL_RECORDING_COMPARISON_CATEGORIES, EXTERNAL_RECORDING_DETAIL_SCOPES, ExternalRecordingCandidateFetchService, ExternalRecordingComparisonService,
    ExternalRecordingDetailFetchService, ExternalRecordingDetailTargetProjectionService, ExternalRecordingEvidenceFetchService, ExternalRecordingTargetProjectionService,
)
from spotify_playlist_taxonomy.workflows.deterministic_match import (
    TIER_A_POLICY_ID, TierAEligibilityService,
)
from spotify_playlist_taxonomy.workflows.research import (
    EvidenceSufficiencyService,
    ResearchCoordinator,
)
from spotify_playlist_taxonomy.workflows.phase6 import Phase6Service
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase8 import Phase8Service
from spotify_playlist_taxonomy.workflows.phase9 import Phase9Service
from spotify_playlist_taxonomy.workflows.phase10 import Phase10Service, ProvisioningError, RecoveryRequiredError
from spotify_playlist_taxonomy.workflows.phase11 import Phase11Service, PopulationError, PopulationRecoveryRequired
from spotify_playlist_taxonomy.workflows.phase12 import Phase12Service

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
catalogue_match_deterministic_app = typer.Typer(no_args_is_help=True, help="Read-only deterministic match-policy analysis.")
catalogue_match_external_recording_app = typer.Typer(no_args_is_help=True, help="Preview local external-recording evidence targets.")
catalogue_match_external_recording_detail_app = typer.Typer(no_args_is_help=True, help="Preview local MusicBrainz Recording-detail targets.")
catalogue_link_app = typer.Typer(no_args_is_help=True, help="Manage explicit canonical identity links.")
research_app = typer.Typer(no_args_is_help=True, help="Gather and inspect independent recording evidence.")
derive_app = typer.Typer(no_args_is_help=True, help="Derive offline characteristics from persisted evidence.")
classify_app = typer.Typer(no_args_is_help=True, help="Evaluate offline semantic proposals from characteristics.")
review_app = typer.Typer(no_args_is_help=True, help="Review local semantic proposals without network access.")
calibration_app = typer.Typer(no_args_is_help=True, help="Report observational local calibration.")
codex_app = typer.Typer(no_args_is_help=True, help="Supervised offline Codex file exchange.")
codex_batch_app = typer.Typer(no_args_is_help=True, help="Create and inspect outbound batches.")
desired_app = typer.Typer(no_args_is_help=True, help="Show local derived concrete target memberships.")
sync_app = typer.Typer(no_args_is_help=True, help="Build local read-only synchronization plans.")
managed_app = typer.Typer(no_args_is_help=True, help="Provision one registered non-collaborative managed playlist at a time.")
managed_provision_app = typer.Typer(no_args_is_help=True, help="Preview or explicitly apply managed playlist provisioning.")
managed_recovery_app = typer.Typer(no_args_is_help=True, help="Explicitly preserve and abandon unknown provisioning attempts.")
populate_app = typer.Typer(no_args_is_help=True, help="Guarded one-track addition to an exact managed playlist.")
populate_recovery_app = typer.Typer(no_args_is_help=True, help="Read-only verification of unresolved managed additions.")
cycle_app = typer.Typer(no_args_is_help=True, help="Local-only V1 operational status.")
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
catalogue_match_app.add_typer(catalogue_match_deterministic_app, name="deterministic")
catalogue_match_app.add_typer(catalogue_match_external_recording_app, name="external-recording")
catalogue_match_external_recording_app.add_typer(catalogue_match_external_recording_detail_app, name="detail")
catalogue_app.add_typer(catalogue_link_app, name="link")
app.add_typer(research_app, name="research")
app.add_typer(derive_app, name="derive")
app.add_typer(classify_app, name="classify")
app.add_typer(review_app, name="review")
app.add_typer(calibration_app, name="calibration")
app.add_typer(codex_app, name="codex")
codex_app.add_typer(codex_batch_app, name="batch")
app.add_typer(desired_app, name="desired")
app.add_typer(sync_app, name="sync")
app.add_typer(managed_app, name="managed")
managed_app.add_typer(managed_provision_app, name="provision")
managed_app.add_typer(managed_recovery_app, name="recovery")
app.add_typer(populate_app, name="populate")
populate_app.add_typer(populate_recovery_app, name="recovery")
app.add_typer(cycle_app, name="cycle")


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
    if isinstance(error, SpotifyAdapterError) and error.kind in {
        SpotifyErrorKind.QUOTA_EXHAUSTED,
        SpotifyErrorKind.RATE_LIMITED,
    }:
        _safe_echo(f"reason: {error.reason or error.kind.value.upper()}", err=True)
        _safe_echo(
            "retry_after_seconds: "
            f"{error.retry_after_seconds if error.retry_after_seconds is not None else 'unavailable'}",
            err=True,
        )
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

@contextmanager
def _phase8_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase8Service]:
    config_path=config_path.resolve(); config,_,_=_load_documents(config_path); paths=_paths(config,data_dir)
    if not paths.database_path.is_file(): raise PersistenceError("database does not exist; run 'db init'")
    connection=connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection)<8: raise PersistenceError("database requires migration 8; run 'db init'")
        yield Phase8Service(connection,load_phase4_bundle(config_path),paths.outbound_batch_dir)
    finally: connection.close()

@contextmanager
def _phase9_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase9Service]:
    config_path = config_path.resolve(); config, _, _ = _load_documents(config_path); paths = _paths(config, data_dir)
    if not paths.database_path.is_file(): raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 8: raise PersistenceError("database requires migration 8; run 'db init'")
        yield Phase9Service(connection, load_phase4_bundle(config_path))
    finally: connection.close()


@contextmanager
def _phase10_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase10Service]:
    config_path = config_path.resolve(); config, _, _ = _load_documents(config_path); paths = _paths(config, data_dir)
    if not paths.database_path.is_file(): raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 13: raise PersistenceError("database requires migration 13; run 'db init'")
        yield Phase10Service(connection, load_phase4_bundle(config_path))
    finally: connection.close()

@contextmanager
def _phase11_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase11Service]:
    config_path = config_path.resolve(); config, _, _ = _load_documents(config_path); paths = _paths(config, data_dir)
    if not paths.database_path.is_file(): raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 14: raise PersistenceError("database requires migration 14; run 'db init'")
        yield Phase11Service(connection, load_phase4_bundle(config_path))
    finally: connection.close()

@contextmanager
def _phase12_service(config_path: Path, data_dir: Path | None) -> Iterator[Phase12Service]:
    config_path = config_path.resolve(); config, _, _ = _load_documents(config_path); paths = _paths(config, data_dir)
    if not paths.database_path.is_file(): raise PersistenceError("database does not exist; run 'db init'")
    connection = connect_database(paths.database_path)
    try:
        assert_integrity(connection)
        if current_schema_version(connection) < 14: raise PersistenceError("database requires migration 14; run 'db init'")
        yield Phase12Service(connection, load_phase4_bundle(config_path))
    finally: connection.close()


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
    managed_provisioning: bool = typer.Option(False, "--managed-provisioning", help="Request the private managed-playlist provisioning scope."),
    managed_population: bool = typer.Option(False, "--managed-population", help="Explicitly request managed population scopes."),
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
            if managed_population:
                token = manager.login(no_browser=no_browser, managed_population=True)
            elif managed_provisioning:
                token = manager.login(no_browser=no_browser, managed_provisioning=True)
            else:
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
    limit: int | None = typer.Option(None, "--limit", min=1, max=100),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """List local matching candidates for explicit review."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            candidates = _phase3_review(store).candidates(state, limit)
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
            review = _phase3_review(store)
            value = review.candidate(candidate_id)
            candidate = value.candidate
            canonical_linked_identities = (
                () if candidate.action is not MatchCandidateAction.LINK_EXISTING
                else review.linked_identity_ids_for_canonical(
                    candidate.canonical_track_id
                )
            )
            open_peers = tuple(
                peer for peer in review.identity(candidate.identity_id).candidates
                if peer.candidate_id != candidate.candidate_id
                and peer.state in {
                    MatchCandidateState.CANDIDATE,
                    MatchCandidateState.AMBIGUOUS,
                    MatchCandidateState.USER_CONFIRMATION_REQUIRED,
                }
            )
        candidate = value.candidate
        _safe_echo(f"candidate_id: {candidate.candidate_id}")
        _safe_echo(f"identity_id: {candidate.identity_id}")
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
        if candidate.action is MatchCandidateAction.LINK_EXISTING:
            _safe_echo(
                "canonical_linked_identities: "
                f"{', '.join(canonical_linked_identities) or '-'}"
            )
        for peer in open_peers:
            _safe_echo(
                f"open_peer: {peer.candidate_id}\taction={peer.action.value}\t"
                f"state={peer.state.value}\tcanonical={peer.canonical_track_id or '-'}"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("evidence-status")
def catalogue_match_evidence_status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show aggregate local evidence counts for unresolved canonical matches."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 15:
                raise PersistenceError("database requires migration 15; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            report = MatchEvidenceService(store, binding.binding_id).report()
        typer.echo("duration_thresholds_cumulative: true")
        for name, value in report.metrics:
            typer.echo(f"{name}: {value}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("evidence-list")
def catalogue_match_evidence_list(
    category: str = typer.Option(..., "--category", help="One of: " + ", ".join(ANOMALY_CATEGORIES)),
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show bounded local evidence anomalies; never contacts Spotify or changes match state."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 15:
                raise PersistenceError("database requires migration 15; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            service = MatchEvidenceReviewService(store, binding.binding_id)
            if category in {"comparison-inconsistent", "multiple-comparisons", "multiple-comparisons-all-agree", "multiple-comparisons-duration-band-mixed"}:
                blocks = service.candidate_details(category, limit)
                for block in blocks:
                    identity = block.comparisons[0]
                    _safe_echo(f"candidate_id: {block.candidate_id}\nidentity_id: {block.identity_id}\nidentity: {identity.identity_title} — {', '.join(identity.identity_artists)}\nidentity_version_cue: {identity.identity_version_cue or '-'}\nidentity_duration_ms: {identity.identity_duration_ms}\nidentity_isrc: {identity.identity_isrc}\ncanonical_id: {block.canonical_track_id}\ncomparison_count: {len(block.comparisons)}\ncomparison_consistent: {str(block.comparison_consistent).lower()}\ncomparison_inconsistent: {str(not block.comparison_consistent).lower()}\nmin_duration_delta_ms: {block.min_duration_delta_ms}\nmax_duration_delta_ms: {block.max_duration_delta_ms}\nduration_band_mixed: {str(block.duration_band_mixed).lower()}")
                    for index, item in enumerate(block.comparisons, 1): _safe_echo(f"comparison_{index}_identity_id: {item.comparison_identity_id}\ncomparison_{index}: {item.comparison_title} — {', '.join(item.comparison_artists)}\ncomparison_{index}_version_cue: {item.comparison_version_cue or '-'}\ncomparison_{index}_duration_ms: {item.comparison_duration_ms}\ncomparison_{index}_isrc: {item.comparison_isrc}\ncomparison_{index}_duration_delta_ms: {item.duration_delta_ms}\ncomparison_{index}_same_isrc: {str(item.identity_isrc == item.comparison_isrc).lower()}\ncomparison_{index}_duration_band: {item.category}")
                return
            rows = service.anomalies(category, limit)
        for item in rows:
            _safe_echo(
                f"candidate_id: {item.candidate_id}\nidentity_id: {item.identity_id}\n"
                f"identity: {item.identity_title} — {', '.join(item.identity_artists)}\n"
                f"identity_version_cue: {item.identity_version_cue or '-'}\n"
                f"identity_duration_ms: {item.identity_duration_ms}\nidentity_isrc: {item.identity_isrc}\n"
                f"canonical_id: {item.canonical_track_id}\ncomparison_identity_id: {item.comparison_identity_id}\n"
                f"comparison: {item.comparison_title} — {', '.join(item.comparison_artists)}\n"
                f"comparison_version_cue: {item.comparison_version_cue or '-'}\n"
                f"comparison_duration_ms: {item.comparison_duration_ms}\ncomparison_isrc: {item.comparison_isrc}\n"
                f"duration_delta_ms: {item.duration_delta_ms}\ncategory: {item.category}\n"
                f"paired_create_new_candidate_id: {item.paired_create_new_candidate_id or '-'}\n"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_external_recording_app.command("preview")
def catalogue_match_external_recording_preview(
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Preview local external-recording targets; it never contacts a provider or writes state."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 17:
                raise PersistenceError("database requires migration 17; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            preview = ExternalRecordingTargetProjectionService(store, binding.binding_id).preview()
        counts = dict(preview.evidence_state_counts)
        for name, value in (
            ("current_candidates", preview.current_candidate_count),
            ("unique_candidate_identities", preview.unique_candidate_identity_count),
            ("unique_comparison_identities", preview.unique_comparison_identity_count),
            ("unique_target_identities", preview.unique_target_identity_count),
            ("candidate_and_comparison_overlap", preview.candidate_and_comparison_overlap),
            ("requires_lookup", preview.requires_lookup_count),
            ("fresh_resolved", counts.get("fresh_resolved", 0)),
            ("fresh_ambiguous", counts.get("fresh_ambiguous", 0)),
            ("fresh_not_found", counts.get("fresh_not_found", 0)),
            ("fresh_insufficient_input", counts.get("fresh_insufficient_input", 0)),
            ("fresh_lookup_failed", counts.get("fresh_lookup_failed", 0)),
            ("stale", counts.get("stale", 0)),
            ("no_evidence", counts.get("no_evidence", 0)),
        ):
            typer.echo(f"{name}: {value}")
        for item in preview.targets[:limit]:
            _safe_echo(
                f"identity_id: {item.identity_id}\nroles: {','.join(item.roles)}\n"
                f"evidence_state: {item.evidence_state}\n"
                f"external_recording_id: {item.external_recording_id or '-'}\n"
                f"fresh: {str(item.is_fresh).lower()}\n"
                f"requires_lookup: {str(item.requires_lookup).lower()}\n"
                f"lookup_reason: {item.lookup_reason}\n"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_external_recording_app.command("fetch")
def catalogue_match_external_recording_fetch(
    limit: int = typer.Option(..., "--limit", min=1, max=100),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly fetch bounded MusicBrainz ISRC recording evidence for current targets."""
    try:
        config_path = config_path.resolve()
        with _phase3_store(config_path, data_dir) as (config, _, store):
            if current_schema_version(store.connection) < 17:
                raise PersistenceError("database requires migration 17; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            policy = _load_research_policy(config_path, config)
            provider_policy = next(item for item in policy.providers if item.provider_id == "musicbrainz" and item.enabled)
            service = ExternalRecordingEvidenceFetchService(
                store, binding.binding_id, positive_ttl_hours=policy.positive_ttl_hours,
                negative_ttl_hours=policy.negative_ttl_hours, ambiguous_ttl_hours=policy.ambiguous_ttl_hours,
            )
            selected = service.plan(limit)
            preview = ExternalRecordingTargetProjectionService(store, binding.binding_id).preview()
            _safe_echo(f"eligible_lookup_targets: {preview.requires_lookup_count}\nselected_targets: {len(selected)}\nprovider: musicbrainz\nentity_type: recording\nlookup_method: isrc\nnetwork_access: yes\nmatching_mutation: no")
            if not yes and not typer.confirm("Fetch bounded MusicBrainz Recording evidence for selected identities?"):
                raise typer.Abort()
            with httpx.Client(timeout=policy.request_timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
                report = service.fetch(limit, MusicBrainzResearchProvider(client=client, policy=provider_policy, lookup_max_attempts=policy.max_attempts))
        for name in ("planned", "processed", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh"):
            _safe_echo(f"{name}: {getattr(report, name)}")
        _safe_echo("matching_mutations: 0")
    except (OSError, StopIteration, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_external_recording_app.command("retry-failed")
def catalogue_match_external_recording_retry_failed(
    limit: int = typer.Option(..., "--limit", min=1, max=100),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly retry only fresh failed MusicBrainz recording lookups."""
    try:
        config_path = config_path.resolve()
        with _phase3_store(config_path, data_dir) as (config, _, store):
            if current_schema_version(store.connection) < 17:
                raise PersistenceError("database requires migration 17; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            policy = _load_research_policy(config_path, config)
            provider_policy = next(item for item in policy.providers if item.provider_id == "musicbrainz" and item.enabled)
            service = ExternalRecordingEvidenceFetchService(
                store, binding.binding_id, positive_ttl_hours=policy.positive_ttl_hours,
                negative_ttl_hours=policy.negative_ttl_hours, ambiguous_ttl_hours=policy.ambiguous_ttl_hours,
            )
            selected = service.failed_retry_plan(limit)
            eligible = sum(item.evidence_state == "fresh_lookup_failed" for item in ExternalRecordingTargetProjectionService(store, binding.binding_id).preview().targets)
            _safe_echo(f"eligible_failed_targets: {eligible}\nselected_targets: {len(selected)}\nprovider: musicbrainz\nentity_type: recording\nlookup_method: isrc\nnetwork_access: yes\nmatching_mutation: no\nrecovery_mode: explicit_lookup_failed_retry")
            if not yes and not typer.confirm("Retry fresh failed MusicBrainz Recording evidence for selected identities?"):
                raise typer.Abort()
            with httpx.Client(timeout=policy.request_timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
                report = service.retry_failed(limit, MusicBrainzResearchProvider(client=client, policy=provider_policy, lookup_max_attempts=policy.max_attempts))
        for name in ("planned", "processed", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_not_failed"):
            _safe_echo(f"{name}: {getattr(report, name)}")
        _safe_echo("matching_mutations: 0")
    except (OSError, StopIteration, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_external_recording_app.command("fetch-candidates")
def catalogue_match_external_recording_fetch_candidates(
    candidate_limit: int = typer.Option(..., "--candidate-limit", min=1, max=20),
    identity_limit: int = typer.Option(..., "--identity-limit", min=1, max=100),
    stratum: str | None = typer.Option(None, "--stratum", help="Optional: " + EXTERNAL_RECORDING_CANDIDATE_STRATUM),
    preview: bool = typer.Option(False, "--preview"),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Fetch complete, bounded unresolved-candidate evidence bundles only."""
    try:
        if preview and yes:
            raise ValueError("--preview cannot be combined with --yes")
        config_path = config_path.resolve()
        with _phase3_store(config_path, data_dir) as (config, _, store):
            if current_schema_version(store.connection) < 17:
                raise PersistenceError("database requires migration 17; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            policy = _load_research_policy(config_path, config)
            provider_policy = next(item for item in policy.providers if item.provider_id == "musicbrainz" and item.enabled)
            service = ExternalRecordingCandidateFetchService(
                store, binding.binding_id, positive_ttl_hours=policy.positive_ttl_hours,
                negative_ttl_hours=policy.negative_ttl_hours, ambiguous_ttl_hours=policy.ambiguous_ttl_hours,
            )
            plan = service.plan(candidate_limit, identity_limit, stratum=stratum)
            plan_output = (
                f"completable_partial_candidates: {plan.completable_partial_candidates}\n"
                f"completable_no_resolution_candidates: {plan.completable_no_resolution_candidates}\n"
                f"blocked_candidates: {plan.blocked_candidates}\nselected_candidates: {len(plan.selected_bundles)}\n"
                f"selected_partial_candidates: {sum(item.primary_outcome == 'partial-resolution' for item in plan.selected_bundles)}\n"
                f"selected_no_resolution_candidates: {sum(item.primary_outcome == 'no-resolution' for item in plan.selected_bundles)}\n"
                f"selected_member_identities: {plan.selected_member_identities}\nalready_resolved_members: {plan.already_resolved_members}\n"
                f"required_lookup_identities: {len(plan.required_lookup_identity_ids)}\ncandidate_limit: {candidate_limit}\nidentity_limit: {identity_limit}\n"
                f"provider: musicbrainz\nentity_type: recording\nlookup_method: isrc\nnetwork_access: {'no' if preview else 'yes'}\nmatching_mutation: no"
            )
            if plan.stratum is not None:
                plan_output += (
                    f"\nstratum: {plan.stratum}\nstratum_matching_candidates: {plan.stratum_matching_candidates}"
                    f"\nstratum_completable_candidates: {plan.stratum_completable_candidates}"
                    f"\nstratum_blocked_candidates: {plan.stratum_blocked_candidates}"
                    f"\nstratum_completed_candidates: {plan.stratum_completed_candidates}"
                    f"\nstratum_completable_required_lookup_identities: {len(plan.stratum_completable_required_lookup_identity_ids)}"
                )
            _safe_echo(plan_output)
            if plan.exceeds_identity_limit:
                _safe_echo("reason: candidate_bundle_exceeds_identity_limit")
                raise typer.Exit(code=2)
            if preview:
                return
            if not yes and not typer.confirm("Fetch MusicBrainz Recording evidence for complete selected candidate bundles?"):
                raise typer.Abort()
            with httpx.Client(timeout=policy.request_timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
                report = service.fetch(plan, MusicBrainzResearchProvider(client=client, policy=provider_policy, lookup_max_attempts=policy.max_attempts))
        report_names = ["planned_candidates", "processed_candidates", "planned_lookup_identities", "processed_lookup_identities", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh", "skipped_candidate_not_completable"]
        if stratum is not None:
            report_names.append("skipped_candidate_not_in_stratum")
        for name in report_names:
            _safe_echo(f"{name}: {getattr(report, name)}")
        _safe_echo("matching_mutations: 0")
    except (OSError, StopIteration, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_external_recording_app.command("status")
def catalogue_match_external_recording_status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Report local external-recording evidence and candidate outcomes without provider access."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 17: raise PersistenceError("database requires migration 17; run 'db init'")
            binding = store.active_account()
            if binding is None: raise Phase3StateError("an active Spotify account binding is required")
            report = ExternalRecordingComparisonService(store, binding.binding_id).status()
        preview = report.target_preview; states = dict(preview.evidence_state_counts); primary = dict(report.primary_counts)
        for name, value in (("current_candidates", preview.current_candidate_count), ("unique_candidate_identities", preview.unique_candidate_identity_count), ("unique_comparison_identities", preview.unique_comparison_identity_count), ("unique_target_identities", preview.unique_target_identity_count), ("requires_lookup", preview.requires_lookup_count), ("fresh_resolved", states.get("fresh_resolved", 0)), ("fresh_ambiguous", states.get("fresh_ambiguous", 0)), ("fresh_not_found", states.get("fresh_not_found", 0)), ("fresh_insufficient_input", states.get("fresh_insufficient_input", 0)), ("fresh_lookup_failed", states.get("fresh_lookup_failed", 0)), ("stale", states.get("stale", 0)), ("no_evidence", states.get("no_evidence", 0)), ("all_resolved_same_recording", primary.get("all-resolved-same-recording", 0)), ("all_resolved_disagree", primary.get("all-resolved-disagree", 0)), ("partial_resolution", primary.get("partial-resolution", 0)), ("no_resolution", primary.get("no-resolution", 0))): typer.echo(f"{name}: {value}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error: _fail(error)


@catalogue_match_external_recording_app.command("compare")
def catalogue_match_external_recording_compare(
    category: str = typer.Option(..., "--category", help="One of: " + ", ".join(EXTERNAL_RECORDING_COMPARISON_CATEGORIES)),
    limit: int = typer.Option(..., "--limit", min=1, max=100),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show bounded local candidate external-recording comparisons; never writes or calls providers."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 17: raise PersistenceError("database requires migration 17; run 'db init'")
            binding = store.active_account()
            if binding is None: raise Phase3StateError("an active Spotify account binding is required")
            rows = ExternalRecordingComparisonService(store, binding.binding_id).compare(category, limit)
        for item in rows:
            candidate = item.candidate
            _safe_echo(
                f"candidate_id: {item.candidate_id}\ncanonical_id: {item.canonical_track_id}\nprimary_outcome: {item.primary_outcome}\n"
                f"member_count: {len(item.members)}\nresolved_identity_count: {item.resolved_identity_count}\n"
                f"distinct_resolved_recording_count: {item.distinct_resolved_recording_count}\n"
                f"candidate_identity_id: {candidate.identity_id}\ncandidate_title: {candidate.title or '-'}\n"
                f"candidate_artists: {', '.join(candidate.artists) or '-'}\n"
                f"candidate_effective_version_cue: {candidate.effective_version_cue or '-'}\n"
                f"candidate_duration_ms: {candidate.duration_ms if candidate.duration_ms is not None else '-'}\n"
                f"candidate_isrc: {candidate.isrc or '-'}\ncandidate_evidence_state: {candidate.evidence_state}\n"
                f"candidate_external_recording_id: {candidate.external_recording_id or '-'}\n"
                f"comparison_consistency: {'inconsistent' if item.comparison_consistency.comparison_inconsistent else 'consistent'}"
            )
            for index, comparison in enumerate(item.comparisons, 1):
                _safe_echo(
                    f"comparison_{index}_identity_id: {comparison.identity_id}\ncomparison_{index}_title: {comparison.title or '-'}\n"
                    f"comparison_{index}_artists: {', '.join(comparison.artists) or '-'}\n"
                    f"comparison_{index}_effective_version_cue: {comparison.effective_version_cue or '-'}\n"
                    f"comparison_{index}_duration_ms: {comparison.duration_ms if comparison.duration_ms is not None else '-'}\n"
                    f"comparison_{index}_isrc: {comparison.isrc or '-'}\n"
                    f"comparison_{index}_evidence_state: {comparison.evidence_state}\n"
                    f"comparison_{index}_external_recording_id: {comparison.external_recording_id or '-'}\n"
                    f"comparison_{index}_duration_delta_ms: {comparison.duration_delta_ms if comparison.duration_delta_ms is not None else '-'}\n"
                    f"comparison_{index}_isrc_relationship: {comparison.isrc_relationship}\n"
                    f"comparison_{index}_version_relationship: {comparison.version_relationship}\n"
                    f"comparison_{index}_comparison_consistency: {comparison.comparison_consistency}\n"
                    f"comparison_{index}_external_recording_relationship: {comparison.external_recording_relationship}"
                )
                for detail_index, detail in enumerate(comparison.recording_detail_contexts, 1):
                    _safe_echo(
                        f"comparison_{index}_recording_detail_{detail_index}_recording_id: {detail.recording_id}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_evidence_state: {detail.detail_evidence_state}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_isrcs_available: {str(detail.isrcs_available).lower() if detail.isrcs_available is not None else '-'}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_associated_isrc_count: {detail.associated_isrc_count if detail.associated_isrc_count is not None else '-'}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_counterpart_isrc_relationship: {detail.counterpart_isrc_relationship}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_candidate_isrc_membership: {detail.candidate_isrc_membership}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_comparison_isrc_membership: {detail.comparison_isrc_membership}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_recording_length_ms: {detail.recording_length_ms if detail.recording_length_ms is not None else '-'}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_candidate_length_delta_ms: {detail.candidate_length_delta_ms if detail.candidate_length_delta_ms is not None else '-'}\n"
                        f"comparison_{index}_recording_detail_{detail_index}_comparison_length_delta_ms: {detail.comparison_length_delta_ms if detail.comparison_length_delta_ms is not None else '-'}"
                    )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error: _fail(error)


@catalogue_match_external_recording_detail_app.command("preview")
def catalogue_match_external_recording_detail_preview(
    limit: int = typer.Option(..., "--limit", min=1, max=20),
    scope: str = typer.Option("partial", "--scope", help="Detail target scope: " + ", ".join(EXTERNAL_RECORDING_DETAIL_SCOPES)),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show bounded local Recording-detail targets without providers or writes."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 18:
                raise PersistenceError("database requires migration 18; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            service = ExternalRecordingDetailTargetProjectionService(store, binding.binding_id, scope=scope)
            preview = service.preview()
            selected = service.selected(limit)
        _safe_echo(
            f"eligible_candidate_references: {preview.eligible_candidate_references}\n"
            f"unique_recording_targets: {preview.unique_recording_targets}\n"
            f"fresh_not_found_counterpart_targets: {preview.fresh_not_found_counterpart_targets}\n"
            f"fresh_ambiguous_counterpart_targets: {preview.fresh_ambiguous_counterpart_targets}\n"
            f"requires_lookup_targets: {preview.requires_lookup_targets}\n"
            f"fresh_resolved_detail: {preview.fresh_resolved_detail}\n"
            f"fresh_not_found_detail: {preview.fresh_not_found_detail}\n"
            f"fresh_lookup_failed_detail: {preview.fresh_lookup_failed_detail}\n"
            f"stale_detail: {preview.stale_detail}\n"
            f"no_detail_evidence: {preview.no_detail_evidence}\n"
            f"selected_targets: {len(selected)}\n"
            f"selected_not_found_counterpart_targets: {sum(item.counterpart_state == 'fresh_not_found' for item in selected)}\n"
            f"selected_ambiguous_counterpart_targets: {sum(item.counterpart_state == 'fresh_ambiguous' for item in selected)}\n"
            f"limit: {limit}\nscope: {scope}\nprovider: musicbrainz\nentity_type: recording\nlookup_method: recording_detail\nnetwork_access: no\nmatching_mutation: no"
        )
        for item in selected:
            _safe_echo(
                f"external_recording_id: {item.external_recording_id}\n"
                f"counterpart_state: {item.counterpart_state}\n"
                f"candidate_reference_count: {item.candidate_reference_count}\n"
                f"detail_evidence_state: {item.detail_evidence_state}\n"
                f"requires_lookup: {str(item.requires_lookup).lower()}"
            )
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_external_recording_detail_app.command("fetch")
def catalogue_match_external_recording_detail_fetch(
    limit: int = typer.Option(..., "--limit", min=1, max=20),
    scope: str = typer.Option("partial", "--scope", help="Detail target scope: " + ", ".join(EXTERNAL_RECORDING_DETAIL_SCOPES)),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly fetch bounded MusicBrainz Recording details for current targets."""
    try:
        with _phase3_store(config_path, data_dir) as (config, _, store):
            if current_schema_version(store.connection) < 18:
                raise PersistenceError("database requires migration 18; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            policy = _load_research_policy(config_path, config)
            provider_policy = next(item for item in policy.providers if item.provider_id == "musicbrainz" and item.enabled)
            projection = ExternalRecordingDetailTargetProjectionService(store, binding.binding_id, scope=scope)
            preview = projection.preview()
            service = ExternalRecordingDetailFetchService(store, binding.binding_id, positive_ttl_hours=policy.positive_ttl_hours, negative_ttl_hours=policy.negative_ttl_hours, scope=scope)
            selected = service.plan(limit)
            _safe_echo(
                f"eligible_candidate_references: {preview.eligible_candidate_references}\nunique_recording_targets: {preview.unique_recording_targets}\n"
                f"fresh_not_found_counterpart_targets: {preview.fresh_not_found_counterpart_targets}\nfresh_ambiguous_counterpart_targets: {preview.fresh_ambiguous_counterpart_targets}\n"
                f"requires_lookup_targets: {preview.requires_lookup_targets}\nfresh_resolved_detail: {preview.fresh_resolved_detail}\nfresh_not_found_detail: {preview.fresh_not_found_detail}\n"
                f"fresh_lookup_failed_detail: {preview.fresh_lookup_failed_detail}\nstale_detail: {preview.stale_detail}\nno_detail_evidence: {preview.no_detail_evidence}\n"
                f"selected_targets: {len(selected)}\nselected_not_found_counterpart_targets: {sum(item.counterpart_state == 'fresh_not_found' for item in selected)}\n"
                f"selected_ambiguous_counterpart_targets: {sum(item.counterpart_state == 'fresh_ambiguous' for item in selected)}\nlimit: {limit}\nscope: {scope}\n"
                "provider: musicbrainz\nentity_type: recording\nlookup_method: recording_detail\nnetwork_access: yes\nmatching_mutation: no"
            )
            if not yes and not typer.confirm("Fetch MusicBrainz Recording details for selected distinct Recordings?"):
                raise typer.Abort()
            with httpx.Client(timeout=policy.request_timeout_seconds, verify=True, follow_redirects=False, trust_env=False) as client:
                report = service.fetch(selected, MusicBrainzResearchProvider(client=client, policy=provider_policy, lookup_max_attempts=policy.max_attempts))
        for name in ("planned", "processed", "resolved", "not_found", "lookup_failed", "skipped_not_required", "resolved_with_isrcs", "resolved_available_empty_isrcs", "resolved_isrcs_unavailable", "resolved_with_length", "resolved_without_length"):
            _safe_echo(f"{name}: {getattr(report, name)}")
        _safe_echo("matching_mutations: 0")
    except (OSError, StopIteration, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_deterministic_app.command("preview")
def catalogue_match_deterministic_preview(
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    eligible_only: bool = typer.Option(False, "--eligible-only"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Analyze Tier A eligibility locally; this command cannot apply or create links."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 15:
                raise PersistenceError("database requires migration 15; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            preview = TierAEligibilityService(store, binding.binding_id).preview(limit=limit, eligible_only=eligible_only)
        _safe_echo(f"policy_id: {TIER_A_POLICY_ID}\ntotal_candidates: {preview.total_candidates}\neligible: {preview.eligible}\nblocked: {preview.blocked}\nblocked_reason_counters_nonexclusive: true")
        for reason, count in preview.blocked_reasons: _safe_echo(f"blocked_{reason}: {count}")
        for name in ("eligible_exact_duration", "eligible_duration_le_250ms", "eligible_duration_le_500ms", "eligible_with_explicit_compatible_version_cue", "eligible_with_no_explicit_version_cue", "eligible_multiple_comparisons"):
            _safe_echo(f"{name}: {getattr(preview, name)}")
        for item in preview.rows:
            _safe_echo(f"candidate_id: {item.candidate_id}\neligible: {item.eligible}\nblocked_reasons: {','.join(item.blocked_reasons) or '-'}\nidentity_id: {item.identity_id or '-'}\nidentity: {(item.identity_title or '-')} — {', '.join(item.identity_artists) or '-'}\nidentity_version_cue: {item.identity_version_cue or '-'}\nidentity_duration_ms: {item.identity_duration_ms if item.identity_duration_ms is not None else '-'}\nidentity_isrc: {item.identity_isrc or '-'}\ncanonical_id: {item.canonical_track_id or '-'}")
            for comparison in item.comparisons:
                _safe_echo(f"comparison_identity_id: {comparison.identity_id}\ncomparison: {comparison.title} — {', '.join(comparison.artists)}\ncomparison_version_cue: {comparison.version_cue or '-'}\ncomparison_duration_ms: {comparison.duration_ms if comparison.duration_ms is not None else '-'}\ncomparison_isrc: {comparison.isrc or '-'}\nduration_delta_ms: {comparison.duration_delta_ms if comparison.duration_delta_ms is not None else '-'}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_deterministic_app.command("apply")
def catalogue_match_deterministic_apply(
    limit: int = typer.Option(..., "--limit", min=1, max=100),
    yes: bool = typer.Option(False, "--yes"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Explicitly apply a bounded, locally revalidated Tier A plan."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 16:
                raise PersistenceError("database requires migration 16; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            service = TierAEligibilityService(store, binding.binding_id)
            plan = service.preview(limit=limit, eligible_only=True)
            selected = tuple(item.candidate_id for item in plan.rows)
            _safe_echo(f"policy_id: {TIER_A_POLICY_ID}\neligible_now: {plan.eligible}\nselected_for_apply: {len(selected)}\nlimit: {limit}")
            if not yes and not typer.confirm("Apply this bounded deterministic canonical-link plan?"):
                raise typer.Abort()
            if not selected:
                return
            report = service.apply(selected)
        _safe_echo(f"policy_id: {TIER_A_POLICY_ID}\nselected: {report.selected}\nconfirmed: {report.confirmed}\nskipped_no_longer_eligible: {sum(count for _, count in report.skipped)}\nfailed: {report.failed}\nskip_reason_counters_nonexclusive: true")
        for reason, count in report.skipped:
            _safe_echo(f"skipped_{reason}: {count}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_deterministic_app.command("history")
def catalogue_match_deterministic_history(
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Show locally persisted deterministic Tier A decision provenance."""
    try:
        with _phase3_store(config_path, data_dir) as (_, _, store):
            if current_schema_version(store.connection) < 16:
                raise PersistenceError("database requires migration 16; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            rows = TierAEligibilityService(store, binding.binding_id).history(limit)
        for row in rows:
            _safe_echo(f"policy_id: {row.policy_id}\ndecision_origin: deterministic\ndecided_at: {row.decided_at}\ncandidate_id: {row.candidate_id}\nidentity_id: {row.identity_id}\ncanonical_id: {row.canonical_track_id}\nevidence_summary: {row.evidence_summary}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError) as error:
        _fail(error)


@catalogue_match_app.command("evidence-backfill")
def catalogue_match_evidence_backfill(
    yes: bool = typer.Option(False, "--yes", help="Confirm Spotify reads and local recording-evidence updates."),
    client_id: str | None = typer.Option(None, "--client-id"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False),
    data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False),
) -> None:
    """Backfill recording evidence for current unresolved link-existing comparisons only."""
    try:
        with _phase3_store(config_path, data_dir) as (config, _, store):
            if current_schema_version(store.connection) < 15:
                raise PersistenceError("database requires migration 15; run 'db init'")
            binding = store.active_account()
            if binding is None:
                raise Phase3StateError("an active Spotify account binding is required")
            service = MatchEvidenceBackfillService(store, binding.binding_id)
            plan = service.plan()
            _safe_echo(f"target_identities: {plan.target_identities}")
            _safe_echo(f"already_complete: {plan.already_complete}")
            _safe_echo(f"needs_backfill: {plan.needs_backfill}")
            if not yes and not typer.confirm("Read selected Spotify tracks and store only duration/ISRC evidence?"):
                raise typer.Abort()
            resolved = resolve_client_id(client_id)
            with _spotify_http_client(config) as http_client:
                manager = _spotify_auth_manager(config, resolved, http_client)
                reader = SpotifyReadOnlyAdapter(
                    token_provider=manager, http_client=http_client,
                    max_read_attempts=config.spotify.max_read_attempts,
                )
                service.require_matching_account(reader.read_current_account().account_id)
                report = service.backfill(reader)
        for name in (
            "target_identities", "already_had_duration", "already_had_isrc", "requested",
            "updated_duration", "updated_isrc", "updated_both", "provider_missing_or_unavailable",
            "failed", "remaining_duration_missing", "remaining_isrc_missing",
        ):
            _safe_echo(f"{name}: {getattr(report, name)}")
    except (OSError, ValueError, ValidationError, PersistenceError, Phase3StateError, SpotifyAdapterError) as error:
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
    _safe_echo(f"{value.logical_playlist_id}\tgenerated={value.generated or '-'}\tsources={','.join(value.generated_sources) or '-'}\texplicit={value.explicit or '-'}\teffective={value.effective or '-'}\tneeds_review={value.needs_review}\tstale={value.stale}")


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

@codex_batch_app.command("create")
def codex_batch_create(canonical_ids: list[str]=typer.Argument(...), config_path: Path=typer.Option(DEFAULT_CONFIG_PATH,"--config",exists=True,dir_okay=False), data_dir: Path|None=typer.Option(None,"--data-dir",file_okay=False)) -> None:
    try:
        with _phase8_service(config_path,data_dir) as service: batch_id,path=service.create(tuple(canonical_ids))
        _safe_echo(f"batch_id: {batch_id}\noutbound_path: {path}")
    except (OSError,LookupError,ValueError,ValidationError,PersistenceError) as error:_fail(error)

@codex_batch_app.command("list")
def codex_batch_list(limit:int=typer.Option(25,"--limit",min=1,max=100),config_path:Path=typer.Option(DEFAULT_CONFIG_PATH,"--config",exists=True,dir_okay=False),data_dir:Path|None=typer.Option(None,"--data-dir",file_okay=False))->None:
    try:
        with _phase8_service(config_path,data_dir) as service: rows=service.list(limit)
        for row in rows:_safe_echo(f"{row['batch_id']}\tstate={row['state']}\titems={row['item_count']}\tproposals={row['proposal_count'] or 0}\timported={row['imported_at'] or '-'}\tschema={row['schema_version']}\ttaxonomy={row['taxonomy_version']}\tdigest={row['outbound_digest']}")
    except (OSError,LookupError,ValueError,ValidationError,PersistenceError) as error:_fail(error)

@codex_batch_app.command("show")
def codex_batch_show(batch_id:str=typer.Argument(...),config_path:Path=typer.Option(DEFAULT_CONFIG_PATH,"--config",exists=True,dir_okay=False),data_dir:Path|None=typer.Option(None,"--data-dir",file_okay=False))->None:
    try:
        with _phase8_service(config_path,data_dir) as service: row,items=service.show(batch_id)
        _safe_echo(f"batch_id: {row['batch_id']}\nstate: {row['state']}\nitems: {row['item_count']}\nproposals: {row['proposal_count'] or 0}\nimported_at: {row['imported_at'] or '-'}\noutbound_path: {row['outbound_path']}")
        for item in items:_safe_echo(f"item_id: {item['item_id']}\tcanonical_ref: {item['canonical_track_id']}")
    except (OSError,LookupError,ValueError,ValidationError,PersistenceError) as error:_fail(error)

@codex_batch_app.command("import")
def codex_batch_import(result_file: Path = typer.Argument(..., exists=True, dir_okay=False), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase8_service(config_path, data_dir) as service: outcome = service.import_result(result_file)
        _safe_echo(f"batch_id: {outcome.batch_id}\nitems: {outcome.item_count}\nproposals: {outcome.proposal_count}\nstate: {outcome.state}\nidempotent: {outcome.idempotent}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)

@desired_app.command("show")
def desired_show(canonical_id: str = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase9_service(config_path, data_dir) as service: state = service.desired(canonical_id)
        _safe_echo(f"canonical_track_id: {state.canonical_track_id}")
        for item in state.memberships:
            _safe_echo(f"semantic={item.semantic_specification_id}\ttarget={item.output_target_id}\trouting={item.routing_outcome or '-'}\treason={item.reason}")
        for item in state.unresolved:
            _safe_echo(f"semantic={item.semantic_specification_id}\ttarget=-\trouting=unresolved\treason={item.reason}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)

@sync_app.command("plan")
def sync_plan(canonical_ids: list[str] = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    try:
        with _phase9_service(config_path, data_dir) as service: plan = service.plan(tuple(canonical_ids))
        for item in plan:
            _safe_echo(f"canonical={item.canonical_track_id}\ttarget={item.output_target_id or '-'}\toutcome={item.outcome.value}\treason={item.reason}")
    except (OSError, LookupError, ValueError, ValidationError, PersistenceError) as error:
        _fail(error)


@managed_app.command("status")
def managed_status(target_id: str = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Show only the durable local registration state; no Spotify call is made."""
    try:
        with _phase10_service(config_path, data_dir) as service: status = service.status(target_id)
        _safe_echo(f"target={status.output_target_id}\tstate={status.state}\tprovider_playlist={mask_identifier(status.provider_playlist_id) if status.provider_playlist_id else '-'}\treason={status.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError) as error: _fail(error)


@managed_provision_app.command("preview")
def managed_provision_preview(target_id: str = typer.Argument(...), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Preview one public=false non-collaborative creation from local state only; never writes."""
    try:
        config, _, _ = _load_documents(config_path); resolved = resolve_client_id(client_id)
        with _spotify_http_client(config) as http_client:
            status = _spotify_auth_manager(config, resolved, http_client).status()
        with _phase10_service(config_path, data_dir) as service:
            binding = service.connection.execute("SELECT binding_id FROM platform_account_bindings WHERE active=1").fetchone()
            result = service.preview(target_id, account_binding_id=None if binding is None else binding["binding_id"], scopes=status.scopes)
        _safe_echo(f"target={result.output_target_id}\tstate={result.state}\treason={result.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, SpotifyAdapterError) as error: _fail(error)


@managed_provision_app.command("apply")
def managed_provision_apply(target_id: str = typer.Argument(...), confirmation: str = typer.Argument(...), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Create one confirmed public=false non-collaborative playlist and register its identity."""
    try:
        config, _, _ = _load_documents(config_path); resolved = resolve_client_id(client_id)
        with _phase10_service(config_path, data_dir) as service:
            binding = service.connection.execute("SELECT binding_id FROM platform_account_bindings WHERE active=1").fetchone()
            binding_id = None if binding is None else binding["binding_id"]
            with _spotify_http_client(config) as http_client:
                manager = _spotify_auth_manager(config, resolved, http_client)
                status = manager.status()
                if PROVISIONING_SCOPE not in status.scopes:
                    raise ProvisioningError("reauthorization with --managed-provisioning is required")
                reader = SpotifyReadOnlyAdapter(token_provider=manager, http_client=http_client, max_read_attempts=config.spotify.max_read_attempts)
                account = reader.read_current_account()
                result = service.apply(target_id, confirmation, account_binding_id=binding_id, account_id=account.account_id, scopes=status.scopes, creator=SpotifyManagedPlaylistProvisioner(token_provider=manager, http_client=http_client))
        _safe_echo(f"target={result.output_target_id}\tstate={result.state}\tprovider_playlist={mask_identifier(result.provider_playlist_id) if result.provider_playlist_id else '-'}\treason={result.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, SpotifyAdapterError, ProvisioningError, RecoveryRequiredError) as error: _fail(error)


@managed_app.command("verify")
def managed_verify(target_id: str = typer.Argument(...), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Read and verify only the exact registered managed playlist; no provider write occurs."""
    try:
        config, _, _ = _load_documents(config_path); resolved = resolve_client_id(client_id)
        with _phase10_service(config_path, data_dir) as service, _spotify_http_client(config) as http_client:
            manager = _spotify_auth_manager(config, resolved, http_client)
            reader = SpotifyReadOnlyAdapter(token_provider=manager, http_client=http_client, max_read_attempts=config.spotify.max_read_attempts)
            result = service.verify_policy(target_id, reader)
        _safe_echo(f"target={result.output_target_id}\tpolicy_confirmed={result.policy_confirmed}\tpublic={result.public_state}\tcollaborative={result.collaborative_state}\tname_matches={result.name_matches}\tdescription_matches={result.description_matches}")
    except (OSError, ValueError, ValidationError, PersistenceError, SpotifyAdapterError, ProvisioningError) as error: _fail(error)




@managed_recovery_app.command("abandon")
def managed_recovery_abandon(target_id: str = typer.Argument(...), confirmation: str = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Locally preserve an unknown attempt after exact operator acknowledgement; no provider call occurs."""
    try:
        with _phase10_service(config_path, data_dir) as service:
            status = service.abandon_unknown(target_id, confirmation)
        _safe_echo(f"target={status.output_target_id}\tstate={status.state}\treason={status.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, ProvisioningError) as error: _fail(error)


def _population_context(config_path: Path, client_id: str | None) -> tuple[ApplicationConfig, str]:
    config, _, _ = _load_documents(config_path)
    return config, resolve_client_id(client_id)


def _populate_operation(service: Phase11Service, target_id: str, canonical_id: str, resolved: str, config: ApplicationConfig, *, apply: bool, confirmation: str = "", recovery: bool = False):
    with _spotify_http_client(config) as http_client:
        manager = _spotify_auth_manager(config, resolved, http_client)
        reader = SpotifyReadOnlyAdapter(token_provider=manager, http_client=http_client, max_read_attempts=config.spotify.max_read_attempts)
        account = reader.read_current_account()
        binding = service.connection.execute("SELECT binding_id FROM platform_account_bindings WHERE active=1").fetchone()
        kwargs = {"account_binding_id": None if binding is None else binding["binding_id"], "account_id": account.account_id, "scopes": manager.status().scopes, "reader": reader}
        if recovery:
            return service.recover_verify(target_id, canonical_id, confirmation, **kwargs)
        if apply:
            return service.apply(target_id, canonical_id, confirmation, adder=SpotifyManagedTrackAdder(token_provider=manager, http_client=http_client), **kwargs)
        return service.preview(target_id, canonical_id, **kwargs)


@populate_app.command("status")
def populate_status(target_id: str = typer.Argument(...), canonical_id: str = typer.Argument(...), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Show local addition-attempt state only; no provider call is made."""
    try:
        with _phase11_service(config_path, data_dir) as service: result = service.status(target_id, canonical_id)
        _safe_echo(f"target={result.output_target_id}\tcanonical={result.canonical_track_id}\tstate={result.state}\tmembership_count={result.membership_count if result.membership_count is not None else '-'}\treason={result.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, PopulationError) as error: _fail(error)


@populate_app.command("preview")
def populate_preview(target_id: str = typer.Argument(...), canonical_id: str = typer.Argument(...), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Read exact managed state for one potential addition; never writes."""
    try:
        config, resolved = _population_context(config_path, client_id)
        with _phase11_service(config_path, data_dir) as service: result = _populate_operation(service, target_id, canonical_id, resolved, config, apply=False)
        _safe_echo(f"target={result.output_target_id}\tcanonical={result.canonical_track_id}\tstate={result.state}\tmembership_count={result.membership_count if result.membership_count is not None else '-'}\trequired_scopes={' '.join(result.required_scopes) or '-'}\treason={result.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, SpotifyAdapterError, PopulationError) as error: _fail(error)


@populate_app.command("apply")
def populate_apply(target_id: str = typer.Argument(...), canonical_id: str = typer.Argument(...), confirmation: str = typer.Argument(...), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Append exactly one confirmed desired track to its exact managed destination."""
    try:
        config, resolved = _population_context(config_path, client_id)
        with _phase11_service(config_path, data_dir) as service: result = _populate_operation(service, target_id, canonical_id, resolved, config, apply=True, confirmation=confirmation)
        _safe_echo(f"target={result.output_target_id}\tcanonical={result.canonical_track_id}\tstate={result.state}\tmembership_count={result.membership_count if result.membership_count is not None else '-'}\treason={result.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, SpotifyAdapterError, PopulationError, PopulationRecoveryRequired) as error: _fail(error)


@populate_recovery_app.command("verify")
def populate_recovery_verify(target_id: str = typer.Argument(...), canonical_id: str = typer.Argument(...), confirmation: str = typer.Argument(...), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Read-only verification of an unresolved one-track managed addition."""
    try:
        config, resolved = _population_context(config_path, client_id)
        with _phase11_service(config_path, data_dir) as service: result = _populate_operation(service, target_id, canonical_id, resolved, config, apply=False, confirmation=confirmation, recovery=True)
        _safe_echo(f"target={result.output_target_id}\tcanonical={result.canonical_track_id}\tstate={result.state}\tmembership_count={result.membership_count if result.membership_count is not None else '-'}\treason={result.reason}")
    except (OSError, ValueError, ValidationError, PersistenceError, SpotifyAdapterError, PopulationError) as error: _fail(error)


@cycle_app.command("status")
def cycle_status(limit: int = typer.Option(10, "--limit", min=1, max=100), client_id: str | None = typer.Option(None, "--client-id"), config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", exists=True, dir_okay=False), data_dir: Path | None = typer.Option(None, "--data-dir", file_okay=False)) -> None:
    """Local-only aggregate status; never contacts a provider or changes state."""
    try:
        with _phase12_service(config_path, data_dir) as service: report = service.status(limit)
        try:
            config, _, _ = _load_documents(config_path); resolved = resolve_client_id(client_id)
            with _spotify_http_client(config) as http_client: auth = _spotify_auth_manager(config, resolved, http_client).status()
            scopes = set(auth.scopes); missing = ",".join(sorted(set(POPULATION_SCOPES) - scopes)) if auth.authorized else "authorization_absent"
            auth_line = f"locally_authorized={auth.authorized}\tstored_scopes={','.join(auth.scopes) or '-'}\tpopulation_scopes_missing={missing or '-'}"
        except (SpotifyAdapterError, ValueError):
            auth_line = "locally_authorized=not_inspected\tstored_scopes=-\tpopulation_scopes_missing=unknown"
        _safe_echo(f"account={report.account_state}\tlive_validity_not_checked\t{auth_line}")
        for item in report.categories: _safe_echo(f"{item.name}\tcount={item.count}\tids={','.join(item.identifiers) or '-'}")
    except (OSError, ValueError, ValidationError, PersistenceError) as error: _fail(error)
