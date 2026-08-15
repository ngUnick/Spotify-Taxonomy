"""Pydantic schemas for repository and local non-secret configuration."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    # YAML represents paths and string-valued enums as strings.  Keep the
    # document shape strict while allowing Pydantic to parse those declared
    # field types from their normal YAML representation.
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PlaylistDimension(StrEnum):
    GENRE = "genre"
    VIBE = "vibe"
    ACTIVITY = "activity"


class SpecificationStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    RETIRED = "retired"


class SpotifyRuntimeConfig(StrictModel):
    """Non-secret Spotify transport settings."""

    request_timeout_seconds: float = Field(default=20, ge=1, le=120)
    max_read_attempts: int = Field(default=3, ge=1, le=10)
    callback_timeout_seconds: float = Field(default=300, ge=30, le=900)


class ApplicationConfig(StrictModel):
    schema_version: Literal["2"]
    taxonomy_path: Path
    database_filename: str = "app.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file_logging: bool = False
    backup_retention_count: int = Field(default=5, ge=1, le=100)
    spotify: SpotifyRuntimeConfig = Field(default_factory=SpotifyRuntimeConfig)

    @field_validator("database_filename")
    @classmethod
    def validate_database_filename(cls, value: str) -> str:
        if not value.strip() or Path(value).name != value:
            raise ValueError("database_filename must be a nonblank filename")
        return value


class TaxonomyPlaylistEntry(StrictModel):
    logical_id: str = Field(min_length=1)
    dimension: PlaylistDimension
    display_name: str = Field(min_length=1)
    enabled: bool
    specification_status: SpecificationStatus

    @field_validator("logical_id", "display_name")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_prefix(self) -> "TaxonomyPlaylistEntry":
        if not self.logical_id.startswith(f"{self.dimension.value}."):
            raise ValueError("logical_id prefix must match dimension")
        return self


class TaxonomyDocument(StrictModel):
    schema_version: Literal["1"]
    taxonomy_version: str = Field(min_length=1)
    playlists: list[TaxonomyPlaylistEntry] = Field(min_length=1)

    @field_validator("taxonomy_version")
    @classmethod
    def reject_blank_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("taxonomy_version must not be blank")
        return value

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "TaxonomyDocument":
        ids = [item.logical_id for item in self.playlists]
        if len(ids) != len(set(ids)):
            raise ValueError("logical playlist IDs must be unique")
        return self


class PlaylistSpecificationDocument(StrictModel):
    schema_version: Literal["1"]
    specification_version: str = Field(min_length=1)
    logical_playlist_id: str = Field(min_length=1)
    status: SpecificationStatus
    purpose: str = Field(min_length=1)
    inclusion_criteria: list[str] = Field(default_factory=list)
    exclusion_criteria: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("specification_version", "logical_playlist_id", "purpose")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("inclusion_criteria", "exclusion_criteria", "notes")
    @classmethod
    def reject_blank_or_duplicate_items(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("criteria and notes must not contain blank entries")
        if len(normalized) != len(set(normalized)):
            raise ValueError("criteria and notes must not contain duplicates")
        return values


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return payload


def load_application_config(path: Path) -> ApplicationConfig:
    return ApplicationConfig.model_validate(_load_yaml_mapping(path))


def resolve_taxonomy_path(config_path: Path, config: ApplicationConfig) -> Path:
    path = config.taxonomy_path
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def load_taxonomy_document(path: Path) -> TaxonomyDocument:
    return TaxonomyDocument.model_validate(_load_yaml_mapping(path))


def load_specification_document(path: Path) -> PlaylistSpecificationDocument:
    return PlaylistSpecificationDocument.model_validate(_load_yaml_mapping(path))


def taxonomy_digest(document: TaxonomyDocument) -> str:
    canonical = json.dumps(
        document.model_dump(mode="json"), ensure_ascii=False,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
