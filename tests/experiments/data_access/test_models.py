from __future__ import annotations

import pytest
from pydantic import ValidationError

from dsx.experiments.data_access.canonical import canonical_byte_count, canonical_digest
from dsx.experiments.data_access.models import (
    CaseConfig,
    DataAccessManifest,
    DatasetFormat,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PreparedDataset,
    PricingSnapshot,
    SqlAttempt,
    SqlAttemptOutcome,
    SqlColumn,
    SqlResult,
    TokenPrice,
)
from dsx.experiments.data_access.sql_tool import QUERY_DATA_TOOL_SCHEMA


def test_opaque_packet_is_schema_free_but_cryptographically_committed() -> None:
    packet = OpaquePacket.from_value({"future_module": {"tracing": [1, 2]}})
    assert packet.value == {"future_module": {"tracing": [1, 2]}}
    with pytest.raises(ValidationError, match="digest"):
        OpaquePacket(
            canonical_json=packet.canonical_json,
            digest="0" * 64,
            byte_count=packet.byte_count,
        )


def test_sql_models_require_result_only_for_success_and_bind_its_digest() -> None:
    payload = {"columns": (SqlColumn(name="n", type_name="BIGINT"),), "rows": ((3,),)}
    result = SqlResult(
        evidence_id="sql-123",
        columns=payload["columns"],
        rows=payload["rows"],
        result_digest=canonical_digest(payload),
        byte_count=canonical_byte_count(payload),
    )
    assert SqlAttempt(
        attempt_id="attempt-1",
        sql="SELECT 3",
        outcome=SqlAttemptOutcome.success,
        elapsed_seconds=0.1,
        result=result,
    ).result == result
    with pytest.raises(ValidationError, match="require only a result"):
        SqlAttempt(
            attempt_id="attempt-2",
            sql="SELECT 3",
            outcome=SqlAttemptOutcome.success,
            elapsed_seconds=0.1,
            result=result,
            error_message="no",
        )
    with pytest.raises(ValidationError, match="failed SQL"):
        SqlAttempt(
            attempt_id="attempt-3",
            sql="SELECT 3",
            outcome=SqlAttemptOutcome.execution_error,
            elapsed_seconds=0.1,
            result=result,
        )


def test_manifest_rejects_a_tampered_query_tool_schema_digest() -> None:
    case = CaseConfig(
        case_id="case",
        task_prompt="inspect",
        dataset_path="data.csv",
        dataset_format=DatasetFormat.csv,
        target_column="label",
    )
    model = ModelConfig(model_identifier="test", system_prompt="return JSON")
    pricing = PricingSnapshot(
        input=TokenPrice(usd_per_million_tokens=1),
        output=TokenPrice(usd_per_million_tokens=1),
        source="test",
        effective_date="2026-08-26",
    )
    packet = OpaquePacket.from_value({"packet": 1})
    limits = ExperimentLimits()
    dataset = PreparedDataset(
        database_path="dataset.duckdb",
        source_digest="a" * 64,
        materialized_digest="b" * 64,
        row_count=1,
        column_names=("label",),
    )
    values = {
        "manifest_version": "data-access-v2",
        "case": case,
        "model": model,
        "pricing": pricing,
        "packet": packet,
        "dataset": dataset,
        "case_digest": canonical_digest(case),
        "model_digest": canonical_digest(model),
        "pricing_digest": canonical_digest(pricing),
        "limits_digest": canonical_digest(limits),
        "oracle_digest": canonical_digest({"case_id": "case", "oracle_version": "v1"}),
        "tool_schema_digest": "0" * 64,
    }
    with pytest.raises(ValidationError, match="tool_schema_digest"):
        DataAccessManifest(**values)
    assert canonical_digest(QUERY_DATA_TOOL_SCHEMA) != "0" * 64
    with pytest.raises(ValidationError, match="case_digest"):
        DataAccessManifest(
            **{
                **values,
                "case_digest": "0" * 64,
                "tool_schema_digest": canonical_digest(QUERY_DATA_TOOL_SCHEMA),
            }
        )
    assert DataAccessManifest(
        **{
            **values,
            "manifest_version": "data-access-v2",
            "tool_schema_digest": canonical_digest(QUERY_DATA_TOOL_SCHEMA),
        }
    ).manifest_version == "data-access-v2"
    for version in (None, "data-access-v1"):
        artifact = {
            **values,
            "tool_schema_digest": canonical_digest(QUERY_DATA_TOOL_SCHEMA),
        }
        if version is not None:
            artifact["manifest_version"] = version
        else:
            del artifact["manifest_version"]
        with pytest.raises(ValidationError, match="manifest_version"):
            DataAccessManifest.model_validate(artifact)


def test_packet_result_and_manifest_commitment_validators_reject_every_tamper_path() -> None:
    packet = OpaquePacket.from_value({"a": 1})
    with pytest.raises(ValidationError, match="byte count"):
        OpaquePacket(
            canonical_json=packet.canonical_json,
            digest=packet.digest,
            byte_count=packet.byte_count + 1,
        )
    payload = {"columns": (SqlColumn(name="n", type_name="BIGINT"),), "rows": ((3,),)}
    with pytest.raises(ValidationError, match="result digest"):
        SqlResult(
            evidence_id="sql-123",
            columns=payload["columns"],
            rows=payload["rows"],
            result_digest="0" * 64,
            byte_count=canonical_byte_count(payload),
        )
    with pytest.raises(ValidationError, match="byte count"):
        SqlResult(
            evidence_id="sql-123",
            columns=payload["columns"],
            rows=payload["rows"],
            result_digest=canonical_digest(payload),
            byte_count=0,
        )
    with pytest.raises(ValidationError, match="canonical"):
        SqlResult(
            evidence_id="sql-123",
            columns=payload["columns"],
            rows=(({"bad"},),),
            result_digest="0" * 64,
            byte_count=0,
        )
