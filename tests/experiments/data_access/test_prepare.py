from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from dsx.experiments.context_lift.models import generate_pilot_case
from dsx.experiments.data_access import prepare
from dsx.experiments.data_access.models import (
    CaseConfig,
    DatasetFormat,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import materialize_dataset, prepare_manifest
from dsx.packet import DatasetRef, DsxPacket, PacketModule


def _case(path: Path, fmt: DatasetFormat, *, target: str = "label") -> CaseConfig:
    return CaseConfig(
        case_id="case",
        task_prompt="inspect it",
        dataset_path=str(path),
        dataset_format=fmt,
        target_column=target,
    )


@pytest.mark.parametrize("fmt", [DatasetFormat.csv, DatasetFormat.parquet])
def test_materializes_tabular_sources_without_sampling(tmp_path: Path, fmt: DatasetFormat) -> None:
    source = tmp_path / f"source.{fmt.value}"
    if fmt is DatasetFormat.csv:
        source.write_text("label,value\n0,one\n1,two\n", encoding="utf-8")
    else:
        connection = duckdb.connect()
        connection.execute("CREATE TABLE source(label INTEGER, value VARCHAR)")
        connection.execute("INSERT INTO source VALUES (0, 'one'), (1, 'two')")
        connection.execute("COPY source TO ? (FORMAT PARQUET)", [str(source)])
        connection.close()
    prepared = materialize_dataset(_case(source, fmt), tmp_path / "dataset.duckdb")
    assert prepared.row_count == 2
    assert prepared.column_names == ("label", "value")
    assert len(prepared.materialized_digest) == 64


def test_materializes_current_pilot_case_rows_and_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "pilot.json"
    source.write_text(generate_pilot_case().model_dump_json(), encoding="utf-8")
    db = tmp_path / "dataset.duckdb"
    prepared = materialize_dataset(_case(source, DatasetFormat.pilot_case_json), db)
    assert prepared.row_count == 5000
    assert "row_id" in prepared.column_names
    with pytest.raises(FileExistsError, match="overwrite"):
        materialize_dataset(_case(source, DatasetFormat.pilot_case_json), db)


def test_pilot_json_requires_rows_and_target_must_exist(tmp_path: Path) -> None:
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"rows": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="rows"):
        materialize_dataset(_case(source, DatasetFormat.pilot_case_json), tmp_path / "bad.duckdb")
    csv = tmp_path / "source.csv"
    csv.write_text("x\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="target"):
        materialize_dataset(_case(csv, DatasetFormat.csv), tmp_path / "missing.duckdb")
    assert not (tmp_path / "missing.duckdb").exists()
    with pytest.raises(ValueError, match="does not exist"):
        materialize_dataset(
            _case(tmp_path / "absent.csv", DatasetFormat.csv), tmp_path / "absent.duckdb"
        )
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be read"):
        materialize_dataset(
            _case(malformed, DatasetFormat.pilot_case_json), tmp_path / "malformed.duckdb"
        )
    non_rows = tmp_path / "non-rows.json"
    non_rows.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="rows"):
        materialize_dataset(
            _case(non_rows, DatasetFormat.pilot_case_json), tmp_path / "non-rows.duckdb"
        )


def test_prepare_manifest_uses_supplied_or_default_limits_and_cleans_close_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("label\n1\n", encoding="utf-8")
    original_connect = prepare.duckdb.connect

    class CloseErrorConnection:
        def __init__(self, delegate: duckdb.DuckDBPyConnection) -> None:
            self._delegate = delegate

        def execute(self, *args: object, **kwargs: object) -> duckdb.DuckDBPyConnection:
            return self._delegate.execute(*args, **kwargs)  # type: ignore[arg-type]

        def close(self) -> None:
            self._delegate.close()
            raise duckdb.Error("close")

    def connect(*args: object, **kwargs: object) -> CloseErrorConnection:
        return CloseErrorConnection(original_connect(*args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(prepare.duckdb, "connect", connect)
    materialize_dataset(_case(source, DatasetFormat.csv), tmp_path / "close.duckdb")


def test_prepare_manifest_commits_the_default_limits(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("label\n1\n", encoding="utf-8")
    packet = DsxPacket(
        packet_id="case-v1",
        dataset=DatasetRef(digest="c" * 64),
        modules=(
            PacketModule(
                module_id="population",
                module_type="profile.population",
                schema_version="1",
                content={"rows": 1},
            ),
        ),
    )
    manifest = prepare_manifest(
        case=_case(source, DatasetFormat.csv),
        packet=OpaquePacket.from_value(packet),
        model=ModelConfig(model_identifier="offline", system_prompt="return JSON"),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=2),
            source="test",
            effective_date="2026-08-26",
        ),
        database_path=tmp_path / "manifest.duckdb",
    )
    assert manifest.limits.repetitions == 3
    assert manifest.packet.value["schema_version"] == "dsx-packet/v1"
