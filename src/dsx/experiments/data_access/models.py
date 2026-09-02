"""Immutable persisted contracts for the Data Access experiment."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import (
    canonical_byte_count,
    canonical_digest,
    canonical_json,
    parse_canonical_json,
)

Digest = str

DEFAULT_SYSTEM_PROMPT = (
    "Return only a valid structured analysis matching the requested response schema."
)
MANIFEST_FILENAME = "manifest.json"
DATABASE_FILENAME = "dataset.duckdb"
RUN_MANIFEST_FILENAME = "run_manifest.json"


class DataAccessContract(BaseModel):
    """Strict base model for every Data Access persisted artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetFormat(StrEnum):
    csv = "csv"
    parquet = "parquet"
    pilot_case_json = "pilot_case_json"


class Arm(StrEnum):
    dsx_packet = "dsx_packet"
    full_data = "full_data"
    packet_and_full_data = "packet_and_full_data"


class SqlAttemptOutcome(StrEnum):
    success = "success"
    invalid_argument = "invalid_argument"
    policy_rejected = "policy_rejected"
    execution_error = "execution_error"
    timeout = "timeout"
    result_too_large = "result_too_large"


class CaseConfig(DataAccessContract):
    """Reusable case definition; the dataset stays external to the packet."""

    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    task_prompt: str = Field(min_length=1)
    dataset_path: str = Field(min_length=1)
    dataset_format: DatasetFormat
    target_column: str = Field(min_length=1)
    oracle_version: str = Field(default="v1", min_length=1)


class ModelConfig(DataAccessContract):
    model_identifier: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    reasoning_effort: str | None = None
    service_tier: str | None = None
    max_output_tokens: int = Field(default=4096, gt=0)


class ExperimentLimits(DataAccessContract):
    repetitions: int = Field(default=3, gt=0)
    model_calls_per_arm: int = Field(default=16, gt=0)
    sql_attempts_per_arm: int = Field(default=12, gt=0)
    arm_wall_clock_seconds: float = Field(default=600.0, gt=0)
    sql_timeout_seconds: float = Field(default=10.0, gt=0)
    sql_max_rows: int = Field(default=1000, gt=0)
    sql_max_result_bytes: int = Field(default=64 * 1024, gt=0)
    parallel_tool_calls: Literal[False] = False


class TokenPrice(DataAccessContract):
    """USD price per one million tokens for one provider usage bucket."""

    usd_per_million_tokens: float = Field(ge=0.0)


class PricingSnapshot(DataAccessContract):
    """Operator-supplied, frozen model pricing used only for estimates."""

    currency: Literal["USD"] = "USD"
    input: TokenPrice
    cached_input: TokenPrice = Field(default_factory=lambda: TokenPrice(usd_per_million_tokens=0.0))
    output: TokenPrice
    reasoning: TokenPrice = Field(default_factory=lambda: TokenPrice(usd_per_million_tokens=0.0))
    source: str = Field(min_length=1)
    effective_date: str = Field(min_length=1)


class PacketBuildMetrics(DataAccessContract):
    elapsed_seconds: float | None = Field(default=None, ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)


class OpaquePacket(DataAccessContract):
    """Canonical packet bytes committed without imposing a packet schema."""

    canonical_json: str = Field(min_length=1)
    digest: Digest = Field(min_length=64, max_length=64)
    byte_count: int = Field(gt=0)

    @classmethod
    def from_value(cls, value: Any) -> OpaquePacket:
        payload = canonical_json(value)
        return cls(
            canonical_json=payload,
            digest=canonical_digest(value),
            byte_count=len(payload.encode("utf-8")),
        )

    @property
    def value(self) -> Any:
        return parse_canonical_json(self.canonical_json)

    @model_validator(mode="after")
    def validate_commitment(self) -> OpaquePacket:
        parsed = parse_canonical_json(self.canonical_json)
        if self.digest != canonical_digest(parsed):
            raise ValueError("packet digest does not match canonical JSON")
        if self.byte_count != canonical_byte_count(parsed):
            raise ValueError("packet byte count does not match canonical JSON")
        return self


class PreparedDataset(DataAccessContract):
    database_path: str = Field(min_length=1)
    source_digest: Digest = Field(min_length=64, max_length=64)
    materialized_digest: Digest = Field(min_length=64, max_length=64)
    row_count: int = Field(ge=0)
    column_names: tuple[str, ...]


class DataAccessManifest(DataAccessContract):
    """Immutable preparation commitments consumed by later execution stages."""

    experiment_name: Literal["Data Access"] = "Data Access"
    case: CaseConfig
    model: ModelConfig
    pricing: PricingSnapshot
    limits: ExperimentLimits = Field(default_factory=ExperimentLimits)
    packet: OpaquePacket
    dataset: PreparedDataset
    packet_build_metrics: PacketBuildMetrics | None = None
    case_digest: Digest = Field(min_length=64, max_length=64)
    model_digest: Digest = Field(min_length=64, max_length=64)
    pricing_digest: Digest = Field(min_length=64, max_length=64)
    limits_digest: Digest = Field(min_length=64, max_length=64)
    tool_schema_digest: Digest = Field(min_length=64, max_length=64)
    oracle_digest: Digest = Field(min_length=64, max_length=64)
    manifest_version: Literal["data-access-v2"]

    @model_validator(mode="after")
    def validate_digests(self) -> DataAccessManifest:
        expected = {
            "case_digest": canonical_digest(self.case),
            "model_digest": canonical_digest(self.model),
            "pricing_digest": canonical_digest(self.pricing),
            "limits_digest": canonical_digest(self.limits),
            "oracle_digest": canonical_digest(
                {"case_id": self.case.case_id, "oracle_version": self.case.oracle_version}
            ),
        }
        for field, digest in expected.items():
            if getattr(self, field) != digest:
                raise ValueError(f"{field} does not match its committed artifact")
        # Keep the schema import local: sql_tool imports these persisted contracts.
        from .sql_tool import QUERY_DATA_TOOL_SCHEMA

        if self.tool_schema_digest != canonical_digest(QUERY_DATA_TOOL_SCHEMA):
            raise ValueError("tool_schema_digest does not match the query tool schema")
        return self


class SqlColumn(DataAccessContract):
    name: str = Field(min_length=1)
    type_name: str = Field(min_length=1)


class SqlResult(DataAccessContract):
    evidence_id: str = Field(min_length=1)
    columns: tuple[SqlColumn, ...]
    rows: tuple[tuple[Any, ...], ...]
    result_digest: Digest = Field(min_length=64, max_length=64)
    byte_count: int = Field(ge=0)

    @field_validator("rows")
    @classmethod
    def validate_rows_are_json(
        cls, rows: tuple[tuple[Any, ...], ...]
    ) -> tuple[tuple[Any, ...], ...]:
        canonical_json(rows)
        return rows

    @model_validator(mode="after")
    def validate_result_commitment(self) -> SqlResult:
        payload = {"columns": self.columns, "rows": self.rows}
        if self.result_digest != canonical_digest(payload):
            raise ValueError("result digest does not match canonical result")
        if self.byte_count != canonical_byte_count(payload):
            raise ValueError("result byte count does not match canonical result")
        return self


class SqlAttempt(DataAccessContract):
    attempt_id: str = Field(min_length=1)
    # Invalid empty input is itself an observable failed discovery attempt.
    sql: str
    outcome: SqlAttemptOutcome
    elapsed_seconds: float = Field(ge=0.0)
    result: SqlResult | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> SqlAttempt:
        if self.outcome is SqlAttemptOutcome.success:
            if self.result is None or self.error_message is not None:
                raise ValueError("successful SQL attempts require only a result")
        elif self.result is not None:
            raise ValueError("failed SQL attempts cannot include a result")
        return self
