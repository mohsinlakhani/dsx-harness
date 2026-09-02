"""Tests for shared tabular and target profiling."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsx.builders.models import ColumnCardinality, ColumnsProfile
from dsx.builders.profiling import (
    class_sort_key,
    json_value,
    profile_table,
    profile_table_and_columns,
    profile_target,
    sha256_file,
)
from tests.builders.helpers import write_csv, write_parquet


def _mixed_rows() -> list[dict[str, object]]:
    return [
        {"feature": "a", "label": "pos", "flag": True, "score": 1.5, "count": 1},
        {"feature": "b", "label": "pos", "flag": False, "score": 2.5, "count": 2},
        {"feature": None, "label": None, "flag": True, "score": None, "count": 3},
        {"feature": "c", "label": "neg", "flag": False, "score": 3.5, "count": 4},
    ]


def _cardinality_rows() -> list[dict[str, object]]:
    return [
        {
            "row_id": 1,
            "duplicate_value": "a",
            "nullable_unique": "x",
            "constant_value": "k",
            "all_null": None,
            "label": "pos",
            'weird "name"': "p",
            "select": "s1",
        },
        {
            "row_id": 2,
            "duplicate_value": "a",
            "nullable_unique": "y",
            "constant_value": "k",
            "all_null": None,
            "label": "neg",
            'weird "name"': "q",
            "select": "s2",
        },
        {
            "row_id": 3,
            "duplicate_value": "b",
            "nullable_unique": None,
            "constant_value": "k",
            "all_null": None,
            "label": "other",
            'weird "name"': "r",
            "select": "s3",
        },
    ]


def _expected_cardinality() -> dict[str, tuple[int, int, float]]:
    return {
        "row_id": (3, 3, 1.0),
        "duplicate_value": (3, 2, 2 / 3),
        "nullable_unique": (2, 2, 2 / 3),
        "constant_value": (3, 1, 1 / 3),
        "all_null": (0, 0, 0.0),
        "label": (3, 3, 1.0),
        'weird "name"': (3, 3, 1.0),
        "select": (3, 3, 1.0),
    }


def test_csv_and_parquet_profiles_are_semantically_equivalent(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "data.csv", _mixed_rows())
    parquet_path = write_parquet(tmp_path / "data.parquet", _mixed_rows())
    csv_profile = profile_table(csv_path, snapshot_id="current")
    parquet_profile = profile_table(parquet_path, snapshot_id="current")
    csv_target = profile_target(csv_path, snapshot_id="current", target_column="label")
    parquet_target = profile_target(
        parquet_path, snapshot_id="current", target_column="label"
    )
    assert csv_profile.row_count == parquet_profile.row_count == 4
    assert [column.name for column in csv_profile.columns] == [
        column.name for column in parquet_profile.columns
    ]
    assert [column.missing_count for column in csv_profile.columns] == [
        column.missing_count for column in parquet_profile.columns
    ]
    assert csv_target is not None and parquet_target is not None
    assert csv_target.null_count == parquet_target.null_count == 1
    assert csv_target.non_null_count == parquet_target.non_null_count == 3
    assert [item.value for item in csv_target.classes] == [
        item.value for item in parquet_target.classes
    ]
    assert [item.count for item in csv_target.classes] == [
        item.count for item in parquet_target.classes
    ]


def test_profile_rejects_unsupported_empty_and_unreadable(tmp_path: Path) -> None:
    json_path = tmp_path / "data.json"
    json_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported dataset format"):
        profile_table(json_path, snapshot_id="current")
    with pytest.raises(ValueError, match="unsupported dataset format"):
        profile_table_and_columns(json_path, snapshot_id="current")
    empty = write_csv(tmp_path / "empty.csv", [])
    with pytest.raises(ValueError, match="empty"):
        profile_table(empty, snapshot_id="current")
    with pytest.raises(ValueError, match="empty"):
        profile_table_and_columns(empty, snapshot_id="current")
    with pytest.raises(ValueError, match="does not exist"):
        profile_table(tmp_path / "missing.csv", snapshot_id="current")
    with pytest.raises(ValueError, match="does not exist"):
        profile_table_and_columns(tmp_path / "missing.csv", snapshot_id="current")
    malformed = tmp_path / "bad.parquet"
    malformed.write_bytes(b"not-a-parquet-file")
    with pytest.raises(ValueError, match="could not parse"):
        profile_table(malformed, snapshot_id="current")
    with pytest.raises(ValueError, match="could not parse"):
        profile_table_and_columns(malformed, snapshot_id="current")


def test_missing_target_and_null_exclusion(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "data.csv", _mixed_rows())
    with pytest.raises(ValueError, match="target column"):
        profile_target(path, snapshot_id="current", target_column="absent")
    assert (
        profile_target(
            path, snapshot_id="current", target_column="absent", require_target=False
        )
        is None
    )
    target = profile_target(path, snapshot_id="current", target_column="label")
    assert target is not None
    assert target.null_count == 1
    rates = [item.rate for item in target.classes]
    assert pytest.approx(sum(rates)) == 1.0
    assert all(item.value is not None for item in target.classes)


def test_class_values_include_string_numeric_boolean_and_sort_deterministically(
    tmp_path: Path,
) -> None:
    # Mixed CSV inference can collapse types; write parquet with explicit types instead.
    import duckdb

    parquet = tmp_path / "typed.parquet"
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE typed(label VARCHAR)")
        connection.execute(
            "INSERT INTO typed VALUES ('true'), ('1'), ('1.5'), ('false'), (NULL)"
        )
        connection.execute("COPY typed TO ? (FORMAT PARQUET)", [str(parquet)])
    finally:
        connection.close()
    string_target = profile_target(parquet, snapshot_id="current", target_column="label")
    assert string_target is not None
    keys = [item.value for item in string_target.classes]
    assert keys == sorted(keys, key=class_sort_key)
    assert string_target.null_count == 1
    numeric = tmp_path / "numeric.parquet"
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE numeric(label INTEGER)")
        connection.execute("INSERT INTO numeric VALUES (1), (2), (2), (NULL)")
        connection.execute("COPY numeric TO ? (FORMAT PARQUET)", [str(numeric)])
        boolean = tmp_path / "boolean.parquet"
        connection.execute("CREATE TABLE boolean(label BOOLEAN)")
        connection.execute("INSERT INTO boolean VALUES (TRUE), (FALSE), (TRUE), (NULL)")
        connection.execute("COPY boolean TO ? (FORMAT PARQUET)", [str(boolean)])
    finally:
        connection.close()
    numbers = profile_target(numeric, snapshot_id="current", target_column="label")
    flags = profile_target(boolean, snapshot_id="current", target_column="label")
    assert numbers is not None and flags is not None
    assert [item.value for item in numbers.classes] == [1, 2]
    assert {item.value for item in flags.classes} == {True, False}
    assert numbers.majority_class_rate == pytest.approx(2 / 3)
    assert flags.majority_class_rate == pytest.approx(2 / 3)


def test_duplicate_class_json_keys_are_merged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_csv(tmp_path / "data.csv", [{"label": "a"}, {"label": "b"}])
    from dsx.builders import profiling

    original_open = profiling._open_table

    class FakeConnection:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def execute(self, sql: str, *args: object, **kwargs: object) -> object:
            if "GROUP BY" in sql:
                return self

            return self._delegate.execute(sql, *args, **kwargs)  # type: ignore[union-attr]

        def fetchall(self) -> list[tuple[object, int]]:
            return [("same", 1), ("same", 2)]

        def close(self) -> None:
            self._delegate.close()  # type: ignore[union-attr]

    def open_table(path: Path, dataset_format: str) -> object:
        return FakeConnection(original_open(path, dataset_format))  # type: ignore[arg-type]

    monkeypatch.setattr(profiling, "_open_table", open_table)
    monkeypatch.setattr(profiling, "_column_names", lambda connection: ("label",))
    target = profiling.profile_target(path, snapshot_id="current", target_column="label")
    assert target is not None
    assert target.classes[0].count == 3


def test_json_value_handles_supported_and_rejected_types() -> None:
    assert json_value(1.5) == 1.5
    assert json_value(Decimal("3")) == 3
    assert json_value(Decimal("1.5")) == 1.5
    assert json_value(datetime(2026, 9, 2, 12, 0, 0)).startswith("2026-09-02")
    assert json_value(date(2026, 9, 2)) == "2026-09-02"
    assert json_value(time(1, 2, 3)) == "01:02:03"
    assert json_value(b"ab") == "6162"
    with pytest.raises(ValueError, match="non-finite"):
        json_value(float("nan"))
    with pytest.raises(ValueError, match="non-finite"):
        json_value(Decimal("Infinity"))
    with pytest.raises(ValueError, match="unsupported JSON value type"):
        json_value(object())
    assert len(sha256_file.__doc__ or "") > 0


def test_csv_and_parquet_column_cardinality_are_semantically_equivalent(tmp_path: Path) -> None:
    rows = _cardinality_rows()
    csv_path = write_csv(tmp_path / "data.csv", rows)
    parquet_path = write_parquet(tmp_path / "data.parquet", rows)
    csv_dataset, csv_columns = profile_table_and_columns(csv_path, snapshot_id="current")
    parquet_dataset, parquet_columns = profile_table_and_columns(
        parquet_path, snapshot_id="current"
    )
    expected_order = list(rows[0].keys())
    assert [column.name for column in csv_columns.columns] == expected_order
    assert [column.name for column in parquet_columns.columns] == expected_order
    expected = _expected_cardinality()
    for csv_column, parquet_column in zip(
        csv_columns.columns, parquet_columns.columns, strict=True
    ):
        non_null, distinct, rate = expected[csv_column.name]
        assert csv_column.non_null_count == parquet_column.non_null_count == non_null
        assert csv_column.distinct_count == parquet_column.distinct_count == distinct
        assert csv_column.uniqueness_rate == parquet_column.uniqueness_rate == rate
    by_name = {column.name: column for column in csv_dataset.columns}
    assert by_name["nullable_unique"].missing_count == 1
    assert by_name["all_null"].missing_count == 3
    assert by_name["row_id"].missing_count == 0


def test_profile_table_wrapper_matches_combined_dataset_profile(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "data.csv", _cardinality_rows())
    wrapped = profile_table(path, snapshot_id="current")
    dataset, columns = profile_table_and_columns(path, snapshot_id="current")
    assert wrapped == dataset
    assert wrapped.current_snapshot_id == dataset.current_snapshot_id == "current"
    assert wrapped.row_count == dataset.row_count == columns.row_count == 3
    assert [column.name for column in wrapped.columns] == [
        column.name for column in dataset.columns
    ]
    assert [column.missing_count for column in wrapped.columns] == [
        column.missing_count for column in dataset.columns
    ]
    assert [column.missing_rate for column in wrapped.columns] == [
        column.missing_rate for column in dataset.columns
    ]
    missing_by_name = {column.name: column.missing_count for column in wrapped.columns}
    assert missing_by_name["row_id"] == 0
    assert missing_by_name["nullable_unique"] == 1
    assert missing_by_name["all_null"] == 3


def test_profile_table_and_columns_wraps_aggregate_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_csv(tmp_path / "data.csv", _cardinality_rows())
    from dsx.builders import profiling

    original_open = profiling._open_table

    class FakeConnection:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def execute(self, sql: str, *args: object, **kwargs: object) -> object:
            if "DISTINCT" in sql:
                raise RuntimeError("aggregate failed")
            return self._delegate.execute(sql, *args, **kwargs)  # type: ignore[union-attr]

        def close(self) -> None:
            self._delegate.close()  # type: ignore[union-attr]

    def open_table(path: Path, dataset_format: str) -> object:
        return FakeConnection(original_open(path, dataset_format))  # type: ignore[arg-type]

    monkeypatch.setattr(profiling, "_open_table", open_table)
    with pytest.raises(ValueError, match="could not parse"):
        profiling.profile_table_and_columns(path, snapshot_id="current")


def test_columns_profile_rejects_duplicate_names_impossible_counts_and_rates() -> None:
    valid = ColumnCardinality(
        name="row_id",
        duckdb_type="BIGINT",
        non_null_count=3,
        distinct_count=3,
        uniqueness_rate=1.0,
    )
    duplicate = ColumnCardinality(
        name="row_id",
        duckdb_type="VARCHAR",
        non_null_count=3,
        distinct_count=1,
        uniqueness_rate=1 / 3,
    )
    with pytest.raises(ValidationError, match="column names must be unique"):
        ColumnsProfile(
            current_snapshot_id="current",
            row_count=3,
            columns=(valid, duplicate),
        )
    with pytest.raises(
        ValidationError, match="distinct_count must be <= non_null_count <= row_count"
    ):
        ColumnsProfile(
            current_snapshot_id="current",
            row_count=3,
            columns=(
                ColumnCardinality(
                    name="too_distinct",
                    duckdb_type="VARCHAR",
                    non_null_count=1,
                    distinct_count=2,
                    uniqueness_rate=2 / 3,
                ),
            ),
        )
    with pytest.raises(
        ValidationError, match="distinct_count must be <= non_null_count <= row_count"
    ):
        ColumnsProfile(
            current_snapshot_id="current",
            row_count=3,
            columns=(
                ColumnCardinality(
                    name="too_populated",
                    duckdb_type="VARCHAR",
                    non_null_count=4,
                    distinct_count=3,
                    uniqueness_rate=1.0,
                ),
            ),
        )
    with pytest.raises(
        ValidationError, match="uniqueness_rate must equal distinct_count / row_count"
    ):
        ColumnsProfile(
            current_snapshot_id="current",
            row_count=3,
            columns=(
                ColumnCardinality(
                    name="row_id",
                    duckdb_type="BIGINT",
                    non_null_count=3,
                    distinct_count=3,
                    uniqueness_rate=0.5,
                ),
            ),
        )
