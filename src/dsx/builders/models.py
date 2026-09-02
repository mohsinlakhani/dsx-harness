"""Immutable packet-builder contracts layered on the stable DSX Packet envelope."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, field_serializer, model_validator

from dsx.packet.models import (
    DsxPacket,
    ModuleName,
    PacketContract,
    PacketId,
    Sha256Digest,
)
from dsx.pipeline import TransformationManifest

CURRENT_SNAPSHOT_ID = "current"
DATASET_PROFILE_MODULE_ID: ModuleName = "dataset-profile"
TARGET_PROFILE_MODULE_ID: ModuleName = "target-profile"
TRANSFORMATION_HISTORY_MODULE_ID: ModuleName = "transformation-history"
DATA_TRAPS_MODULE_ID: ModuleName = "data-traps"
DATASET_PROFILE_TYPE: ModuleName = "profile.dataset"
TARGET_PROFILE_TYPE: ModuleName = "profile.target"
TRANSFORMATION_HISTORY_TYPE: ModuleName = "history.transforms"
DATA_TRAPS_TYPE: ModuleName = "risk.data_traps"
DATASET_PROFILE_SCHEMA = "profile.dataset/v1"
TARGET_PROFILE_SCHEMA = "profile.target/v1"
TRANSFORMATION_HISTORY_SCHEMA = "history.transforms/v1"
DATA_TRAPS_SCHEMA = "risk.data_traps/v1"

TrapKind = Literal[
    "target_class_imbalance",
    "augmentation_before_split",
    "augmentation_on_evaluation",
    "augmentation_changed_target_distribution",
]


class ColumnSummary(PacketContract):
    name: str = Field(min_length=1)
    duckdb_type: str = Field(min_length=1)
    missing_count: int = Field(ge=0)
    missing_rate: float = Field(ge=0.0, le=1.0)


class DatasetProfile(PacketContract):
    current_snapshot_id: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    columns: tuple[ColumnSummary, ...]


class ClassCount(PacketContract):
    value: JsonValue
    count: int = Field(ge=0)
    rate: float = Field(ge=0.0, le=1.0)


class TargetDistribution(PacketContract):
    snapshot_id: str = Field(min_length=1)
    null_count: int = Field(ge=0)
    non_null_count: int = Field(ge=0)
    classes: tuple[ClassCount, ...]
    majority_class_rate: float = Field(ge=0.0, le=1.0)


class AugmentationDistribution(PacketContract):
    step_id: str = Field(min_length=1)
    before: TargetDistribution
    after: TargetDistribution


class TargetProfile(PacketContract):
    target_column: str = Field(min_length=1)
    current_snapshot_id: str = Field(min_length=1)
    null_count: int = Field(ge=0)
    non_null_count: int = Field(ge=0)
    classes: tuple[ClassCount, ...]
    majority_class_rate: float = Field(ge=0.0, le=1.0)
    augmentation_distributions: tuple[AugmentationDistribution, ...] = ()


class HistoryStep(PacketContract):
    step_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    inputs: tuple[str, ...] = Field(min_length=1)
    outputs: tuple[str, ...] = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class TransformationHistory(PacketContract):
    pipeline_id: str = Field(min_length=1)
    manifest_digest: Sha256Digest
    current_snapshot_id: str = Field(min_length=1)
    steps: tuple[HistoryStep, ...]
    accessible_snapshot_ids: tuple[str, ...]
    unavailable_snapshot_ids: tuple[str, ...]


class DataTrap(PacketContract):
    finding_id: str = Field(min_length=1)
    kind: TrapKind
    severity: Literal["warning"] = "warning"
    message: str = Field(min_length=1)
    snapshot_ids: tuple[str, ...] = ()
    step_ids: tuple[str, ...] = ()
    details: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> DataTrap:
        try:
            json.dumps(self.details, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("details must be standard JSON") from error
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs must be unique within a finding")
        if len(set(self.snapshot_ids)) != len(self.snapshot_ids):
            raise ValueError("snapshot_ids must be unique within a finding")
        if len(set(self.step_ids)) != len(self.step_ids):
            raise ValueError("step_ids must be unique within a finding")
        return self


class ModuleDescriptor(PacketContract):
    module_id: ModuleName
    module_type: ModuleName
    schema_version: str = Field(min_length=1, max_length=64)


class PacketBuildRecord(PacketContract):
    schema_version: Literal["dsx-packet-build/v1"] = "dsx-packet-build/v1"
    build_id: str = Field(min_length=1, max_length=128)
    built_at: datetime
    revision: int = Field(ge=1)
    packet_id: PacketId
    packet_digest: Sha256Digest
    previous_packet_digest: Sha256Digest | None = None
    dataset_path: str = Field(min_length=1)
    dataset_digest: Sha256Digest
    target_column: str = Field(min_length=1)
    manifest_digest: Sha256Digest | None = None
    modules: tuple[ModuleDescriptor, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_built_at(self) -> PacketBuildRecord:
        if self.built_at.tzinfo is None or self.built_at.utcoffset() != timedelta(0):
            raise ValueError("built_at must be an aware UTC timestamp")
        return self

    @field_serializer("built_at")
    def serialize_built_at(self, value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PacketBuildRequest(PacketContract):
    dataset_path: Path
    target_column: str = Field(min_length=1)
    packet_id: PacketId
    manifest: TransformationManifest | None = None
    snapshot_root: Path | None = None
    previous_build_record: PacketBuildRecord | None = None
    previous_packet_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def validate_previous_history(self) -> PacketBuildRequest:
        has_record = self.previous_build_record is not None
        has_digest = self.previous_packet_digest is not None
        if has_record != has_digest:
            raise ValueError(
                "previous build record and previous packet digest must be supplied together"
            )
        if (
            self.previous_build_record is not None
            and self.previous_packet_digest is not None
            and self.previous_build_record.packet_digest != self.previous_packet_digest
        ):
            raise ValueError("previous packet digest does not match the previous build record")
        if self.manifest is not None and self.snapshot_root is None:
            for snapshot in self.manifest.snapshots:
                if snapshot.path is None:
                    continue
                if not Path(snapshot.path).is_absolute():
                    raise ValueError(
                        "snapshot_root is required when the manifest declares relative paths"
                    )
        return self


class PacketBuildResult(PacketContract):
    packet: DsxPacket
    build_record: PacketBuildRecord
    manifest: TransformationManifest | None = None
