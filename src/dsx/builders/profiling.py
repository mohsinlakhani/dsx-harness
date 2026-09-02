"""Tabular dataset and target profiling shared by packet module builders."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import JsonValue

from .models import (
    ClassCount,
    ColumnCardinality,
    ColumnsProfile,
    ColumnSummary,
    DatasetProfile,
    TargetDistribution,
)

SupportedFormat = Literal["csv", "parquet"]


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_dataset_format(path: Path) -> SupportedFormat:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".parquet":
        return "parquet"
    raise ValueError(f"unsupported dataset format: {path}")


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def json_value(value: Any) -> JsonValue:
    """Convert DuckDB values to standard JSON without importing experiment code."""
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON contains a non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON contains a non-finite float")
        as_int = int(value)
        if value == as_int:
            return as_int
        as_float = float(value)
        if not math.isfinite(as_float):  # pragma: no cover - huge non-integral decimals
            raise ValueError("canonical JSON contains a non-finite float")
        return as_float
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


def class_sort_key(value: JsonValue) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def count_table_rows(path: Path, dataset_format: SupportedFormat) -> int:
    """Return the row count of a CSV or Parquet table using an explicit format."""
    connection = _open_table(path, dataset_format)
    try:
        return _row_count(connection)
    finally:
        connection.close()


def profile_table(
    path: Path,
    *,
    snapshot_id: str,
    format: SupportedFormat | None = None,
) -> DatasetProfile:
    """Profile row count, types, and missingness for a CSV or Parquet table."""
    dataset_profile, _columns_profile = profile_table_and_columns(
        path, snapshot_id=snapshot_id, format=format
    )
    return dataset_profile


def profile_table_and_columns(
    path: Path,
    *,
    snapshot_id: str,
    format: SupportedFormat | None = None,
) -> tuple[DatasetProfile, ColumnsProfile]:
    """Profile missingness and cardinality from one opened CSV or Parquet table."""
    dataset_format = format or detect_dataset_format(path)
    connection = _open_table(path, dataset_format)
    try:
        try:
            row_count = _row_count(connection)
            if row_count == 0:
                raise ValueError(f"dataset is empty: {path}")
            summaries, cardinalities = _column_summaries_and_cardinalities(connection, row_count)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f"could not parse {dataset_format} dataset: {path}") from error
        return (
            DatasetProfile(
                current_snapshot_id=snapshot_id,
                row_count=row_count,
                columns=tuple(summaries),
            ),
            ColumnsProfile(
                current_snapshot_id=snapshot_id,
                row_count=row_count,
                columns=tuple(cardinalities),
            ),
        )
    finally:
        connection.close()


def profile_target(
    path: Path,
    *,
    snapshot_id: str,
    target_column: str,
    format: SupportedFormat | None = None,
    require_target: bool = True,
) -> TargetDistribution | None:
    """Profile the target column, excluding nulls from class-rate denominators."""
    dataset_format = format or detect_dataset_format(path)
    connection = _open_table(path, dataset_format)
    try:
        names = _column_names(connection)
        if target_column not in names:
            if require_target:
                raise ValueError(f"target column is not present in dataset: {target_column}")
            return None
        quoted = quote_identifier(target_column)
        counts = connection.execute(
            f"SELECT {quoted}, count(*) FROM dataset GROUP BY 1"
        ).fetchall()
        class_counts: dict[str, tuple[JsonValue, int]] = {}
        null_count = 0
        for value, count in counts:
            observed = int(count)
            if value is None:
                null_count += observed
                continue
            json_class = json_value(value)
            key = class_sort_key(json_class)
            existing = class_counts.get(key)
            if existing is None:
                class_counts[key] = (json_class, observed)
            else:
                class_counts[key] = (existing[0], existing[1] + observed)
        non_null_count = sum(item[1] for item in class_counts.values())
        classes = tuple(
            ClassCount(
                value=item[0],
                count=item[1],
                rate=(item[1] / non_null_count) if non_null_count else 0.0,
            )
            for _, item in sorted(class_counts.items(), key=lambda pair: pair[0])
        )
        majority = max((item.count for item in classes), default=0)
        majority_rate = (majority / non_null_count) if non_null_count else 0.0
        return TargetDistribution(
            snapshot_id=snapshot_id,
            null_count=null_count,
            non_null_count=non_null_count,
            classes=classes,
            majority_class_rate=majority_rate,
        )
    finally:
        connection.close()


def _open_table(path: Path, dataset_format: SupportedFormat) -> duckdb.DuckDBPyConnection:
    if not path.is_file():
        raise ValueError(f"dataset does not exist or is unreadable: {path}")
    connection = duckdb.connect()
    try:
        if dataset_format == "csv":
            connection.execute(
                "CREATE TABLE dataset AS SELECT * FROM read_csv_auto(?)", [str(path)]
            )
        else:
            connection.execute(
                "CREATE TABLE dataset AS SELECT * FROM read_parquet(?)", [str(path)]
            )
    except Exception as error:
        connection.close()
        raise ValueError(f"could not parse {dataset_format} dataset: {path}") from error
    return connection


def _row_count(connection: duckdb.DuckDBPyConnection) -> int:
    row = connection.execute("SELECT count(*) FROM dataset").fetchone()
    if row is None:  # pragma: no cover - count always returns one row
        raise RuntimeError("dataset count query did not return a row")
    return int(row[0])


def _column_names(connection: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    return tuple(str(item[0]) for item in connection.execute("DESCRIBE dataset").fetchall())


def _column_summaries_and_cardinalities(
    connection: duckdb.DuckDBPyConnection, row_count: int
) -> tuple[list[ColumnSummary], list[ColumnCardinality]]:
    description = connection.execute("DESCRIBE dataset").fetchall()
    summaries: list[ColumnSummary] = []
    cardinalities: list[ColumnCardinality] = []
    for item in description:
        name = str(item[0])
        duckdb_type = str(item[1])
        quoted = quote_identifier(name)
        counts_row = connection.execute(
            f"SELECT count({quoted}), count(DISTINCT {quoted}) FROM dataset"
        ).fetchone()
        if counts_row is None:  # pragma: no cover - count always returns one row
            raise RuntimeError("column-count query did not return a row")
        non_null_count = int(counts_row[0])
        distinct_count = int(counts_row[1])
        missing_count = row_count - non_null_count
        uniqueness_rate = distinct_count / row_count
        summaries.append(
            ColumnSummary(
                name=name,
                duckdb_type=duckdb_type,
                missing_count=missing_count,
                missing_rate=missing_count / row_count,
            )
        )
        cardinalities.append(
            ColumnCardinality(
                name=name,
                duckdb_type=duckdb_type,
                non_null_count=non_null_count,
                distinct_count=distinct_count,
                uniqueness_rate=uniqueness_rate,
            )
        )
    return summaries, cardinalities


def rates_by_class(distribution: TargetDistribution) -> Mapping[str, float]:
    return {class_sort_key(item.value): item.rate for item in distribution.classes}
