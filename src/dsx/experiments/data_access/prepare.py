"""Dataset materialization and immutable Data Access preparation commitments."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import duckdb

from .canonical import canonical_digest, json_value
from .models import (
    CaseConfig,
    DataAccessManifest,
    DatasetFormat,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PacketBuildMetrics,
    PreparedDataset,
    PricingSnapshot,
)
from .sql_tool import QUERY_DATA_TOOL_SCHEMA


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _read_pilot_rows(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("pilot-case JSON could not be read") from error
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("pilot-case JSON must be an object with a non-empty rows array")
    return rows


def _materialized_payload(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    description = connection.execute("DESCRIBE dataset").fetchall()
    columns = tuple(str(item[0]) for item in description)
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    rows = connection.execute(f"SELECT {quoted_columns} FROM dataset").fetchall()
    return {
        "columns": [
            {"name": str(item[0]), "type_name": str(item[1])}
            for item in description
        ],
        "rows": [list(json_value(row)) for row in rows],
    }


def materialize_dataset(case: CaseConfig, database_path: Path) -> PreparedDataset:
    """Copy a supported source into a prepared DuckDB ``dataset`` table.

    The source is never sampled. ``database_path`` must not already exist so callers
    cannot accidentally overwrite a frozen dataset artifact.
    """
    source = Path(case.dataset_path)
    if not source.is_file():
        raise ValueError(f"dataset source does not exist: {source}")
    if database_path.exists():
        raise FileExistsError(f"refusing to overwrite prepared database: {database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    source_digest = _sha256_file(source)
    # Preparation reads the explicitly supplied local source. The query tool later
    # reopens this database read-only with external access disabled.
    connection = duckdb.connect(str(database_path))
    temporary_json: Path | None = None
    try:
        if case.dataset_format is DatasetFormat.csv:
            connection.execute(
                "CREATE TABLE dataset AS SELECT * FROM read_csv_auto(?)", [str(source)]
            )
        elif case.dataset_format is DatasetFormat.parquet:
            connection.execute(
                "CREATE TABLE dataset AS SELECT * FROM read_parquet(?)", [str(source)]
            )
        elif case.dataset_format is DatasetFormat.pilot_case_json:
            rows = _read_pilot_rows(source)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", delete=False, dir=database_path.parent
            ) as serialized_rows:
                json.dump(rows, serialized_rows, allow_nan=False, separators=(",", ":"))
                temporary_json = Path(serialized_rows.name)
            connection.execute(
                "CREATE TABLE dataset AS SELECT * FROM read_json_auto(?)", [str(temporary_json)]
            )
        else:  # pragma: no cover - enum validation makes this defensive only
            raise ValueError(f"unsupported dataset format: {case.dataset_format}")
        description = connection.execute("DESCRIBE dataset").fetchall()
        columns = tuple(str(item[0]) for item in description)
        if case.target_column not in columns:
            raise ValueError(f"target column is not present in dataset: {case.target_column}")
        count_row = connection.execute("SELECT count(*) FROM dataset").fetchone()
        if count_row is None:  # pragma: no cover - count always returns one row
            raise RuntimeError("dataset count query did not return a row")
        row_count = int(count_row[0])
        payload = _materialized_payload(connection)
        return PreparedDataset(
            database_path=str(database_path),
            source_digest=source_digest,
            materialized_digest=canonical_digest(payload),
            row_count=row_count,
            column_names=columns,
        )
    except Exception:
        connection.close()
        database_path.unlink(missing_ok=True)
        raise
    finally:
        if temporary_json is not None:
            temporary_json.unlink(missing_ok=True)
        try:
            connection.close()
        except duckdb.Error:
            pass


def prepare_manifest(
    *,
    case: CaseConfig,
    packet: OpaquePacket,
    model: ModelConfig,
    pricing: PricingSnapshot,
    database_path: Path,
    limits: ExperimentLimits | None = None,
    packet_build_metrics: PacketBuildMetrics | None = None,
) -> DataAccessManifest:
    """Materialize a case and construct its complete immutable execution manifest."""
    prepared = materialize_dataset(case, database_path)
    active_limits = limits or ExperimentLimits()
    return DataAccessManifest(
        manifest_version="data-access-v2",
        case=case,
        model=model,
        pricing=pricing,
        limits=active_limits,
        packet=packet,
        dataset=prepared,
        packet_build_metrics=packet_build_metrics,
        case_digest=canonical_digest(case),
        model_digest=canonical_digest(model),
        pricing_digest=canonical_digest(pricing),
        limits_digest=canonical_digest(active_limits),
        tool_schema_digest=canonical_digest(QUERY_DATA_TOOL_SCHEMA),
        oracle_digest=canonical_digest(
            {"case_id": case.case_id, "oracle_version": case.oracle_version}
        ),
    )
