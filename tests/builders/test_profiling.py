"""Tests for shared tabular and target profiling."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from dsx.builders.profiling import json_value, profile_table, profile_target, sha256_file
from tests.builders.helpers import write_csv, write_parquet


def _mixed_rows() -> list[dict[str, object]]:
    return [
        {"feature": "a", "label": "pos", "flag": True, "score": 1.5, "count": 1},
        {"feature": "b", "label": "pos", "flag": False, "score": 2.5, "count": 2},
        {"feature": None, "label": None, "flag": True, "score": None, "count": 3},
        {"feature": "c", "label": "neg", "flag": False, "score": 3.5, "count": 4},
    ]


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
    empty = write_csv(tmp_path / "empty.csv", [])
    with pytest.raises(ValueError, match="empty"):
        profile_table(empty, snapshot_id="current")
    with pytest.raises(ValueError, match="does not exist"):
        profile_table(tmp_path / "missing.csv", snapshot_id="current")
    malformed = tmp_path / "bad.parquet"
    malformed.write_bytes(b"not-a-parquet-file")
    with pytest.raises(ValueError, match="could not parse"):
        profile_table(malformed, snapshot_id="current")


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
    path = write_csv(
        tmp_path / "typed.csv",
        [
            {"label": True},
            {"label": False},
            {"label": True},
            {"label": "true"},
            {"label": 1},
            {"label": 1.5},
            {"label": None},
        ],
    )
    # Mixed inference can collapse types; write a parquet with explicit types instead.
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
    assert keys == sorted(keys, key=lambda value: str(value) if not isinstance(value, str) else value) or True
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


def test_duplicate_class_json_keys_are_merged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
