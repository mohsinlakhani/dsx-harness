"""Immutable transformation-manifest contracts for declared dataset history."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from dsx.packet.models import PacketId, Sha256Digest

SnapshotId = PacketId
StepId = PacketId
PipelineId = PacketId


class PipelineContract(BaseModel):
    """Strict immutable base for transformation-history contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SnapshotRole(StrEnum):
    source = "source"
    intermediate = "intermediate"
    train = "train"
    validation = "validation"
    test = "test"


class TransformationOperation(StrEnum):
    filter = "filter"
    join = "join"
    derive = "derive"
    rename = "rename"
    drop = "drop"
    split = "split"
    sample = "sample"
    augment = "augment"
    custom = "custom"


class DatasetFormat(StrEnum):
    csv = "csv"
    parquet = "parquet"


class DatasetSnapshot(PipelineContract):
    """One immutable dataset snapshot declared in a transformation manifest."""

    snapshot_id: SnapshotId
    digest: Sha256Digest
    role: SnapshotRole
    path: str | None = Field(default=None, min_length=1)
    format: DatasetFormat | None = None

    @model_validator(mode="after")
    def validate_path_and_format(self) -> DatasetSnapshot:
        if self.path is not None and self.format is None:
            raise ValueError("format is required when path is present")
        return self


class TransformationStep(PipelineContract):
    """One declared transformation between dataset snapshots."""

    step_id: StepId
    operation: TransformationOperation
    inputs: tuple[SnapshotId, ...] = Field(min_length=1)
    outputs: tuple[SnapshotId, ...] = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self) -> TransformationStep:
        try:
            json.dumps(self.parameters, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("parameters must be standard JSON") from error
        return self


class TransformationManifest(PipelineContract):
    """Declared data-transformation history for a pipeline snapshot."""

    schema_version: Literal["dsx-transform-manifest/v1"] = "dsx-transform-manifest/v1"
    pipeline_id: PipelineId
    current_snapshot_id: SnapshotId
    snapshots: tuple[DatasetSnapshot, ...] = Field(min_length=1)
    steps: tuple[TransformationStep, ...] = ()

    def canonical_json(self) -> str:
        """Return deterministic JSON suitable for persistence and commitments."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        """Return the SHA-256 commitment for the canonical manifest."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
