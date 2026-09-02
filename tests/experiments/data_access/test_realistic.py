from __future__ import annotations

import json
from typing import Any

import pytest

from dsx.builders.build import build_packet
from dsx.builders.models import DatasetProfile, PacketBuildRequest
from dsx.packet import DsxPacket
from tests.builders.helpers import write_csv, write_parquet


def _eligible_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(20):
        rows.append(
            {
                "row_id": f"r{index}",
                "label": 1 if index == 0 else 0,
                "nullable": None if index < 2 else index,
                "noise": index,
            }
        )
    return rows


def _eligible_packet(tmp_path) -> DsxPacket:
    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    return build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="eligible-v1"
        )
    ).packet


def _rebuild_packet(packet: DsxPacket, module_id: str, content: Any) -> DsxPacket:
    payload = json.loads(packet.canonical_json())
    for module in payload["modules"]:
        if module["module_id"] == module_id:
            module["content"] = content
            return DsxPacket.model_validate(payload)
    raise AssertionError(f"missing module {module_id}")


def _module_json(packet: DsxPacket, module_id: str) -> Any:
    payload = json.loads(packet.canonical_json())
    for module in payload["modules"]:
        if module["module_id"] == module_id:
            return module["content"]
    raise AssertionError(f"missing module {module_id}")


def test_inspect_tabular_source_reads_csv_shape(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import inspect_tabular_source

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    inspection = inspect_tabular_source(path, target_column="label")
    assert inspection.row_count == 20
    assert inspection.column_names == ("row_id", "label", "nullable", "noise")
    assert inspection.target_distinct_non_null == 2
    assert inspection.dataset_format.value == "csv"


def test_inspect_rejects_missing_target(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import inspect_tabular_source

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    with pytest.raises(ValueError, match="target column is not present"):
        inspect_tabular_source(path, target_column="missing")


def test_evaluate_eligibility_accepts_identifier_imbalance_and_missingness(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import evaluate_eligibility

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="eligible-v1"
        )
    ).packet
    eligibility = evaluate_eligibility(packet, target_column="label")
    assert "likely_identifier" in eligibility.signals
    assert "target_class_imbalance" in eligibility.signals
    assert "material_missingness" in eligibility.signals
    assert eligibility.row_count == 20
    assert eligibility.column_count == 4


def test_evaluate_eligibility_rejects_oversized_and_signalless_packets(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import (
        MAX_COLUMNS,
        MAX_ROWS,
        evaluate_eligibility,
    )

    path = write_csv(
        tmp_path / "balanced.csv",
        [
            {"label": 0, "noise": 1},
            {"label": 1, "noise": 1},
        ],
    )
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="tiny-v1"
        )
    ).packet
    with pytest.raises(ValueError, match="no eligibility signal"):
        evaluate_eligibility(packet, target_column="label")

    oversized = packet.model_copy(
        update={
            "modules": tuple(
                module.model_copy(
                    update={
                        "content": {**module.content, "row_count": MAX_ROWS + 1}
                        if module.module_id == "dataset-profile"
                        and isinstance(module.content, dict)
                        else module.content
                    }
                )
                for module in packet.modules
            )
        }
    )
    with pytest.raises(ValueError, match="row_count"):
        evaluate_eligibility(oversized, target_column="label")
    assert MAX_COLUMNS == 40


def test_require_review_classifier_task_passes_on_builder_packet(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import require_review_classifier_task

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="eligible-v1"
        )
    ).packet
    assembled = require_review_classifier_task(packet)
    assert assembled.task.task_id == "review-classifier"
    assert len(assembled.modules) == 5


def test_inspect_tabular_source_reads_parquet_shape(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import inspect_tabular_source

    path = write_parquet(tmp_path / "data.parquet", _eligible_rows())
    inspection = inspect_tabular_source(path, target_column="label")
    assert inspection.row_count == 20
    assert inspection.column_names == ("row_id", "label", "nullable", "noise")
    assert inspection.target_distinct_non_null == 2
    assert inspection.dataset_format.value == "parquet"


def test_inspect_rejects_unsupported_format(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import inspect_tabular_source

    path = tmp_path / "data.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported dataset format"):
        inspect_tabular_source(path, target_column="label")


def test_packet_module_content_rejects_missing_module(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import packet_module_content

    packet = _eligible_packet(tmp_path)
    with pytest.raises(ValueError, match="does not contain module"):
        packet_module_content(packet, "missing-module", DatasetProfile)


def test_evaluate_eligibility_rejects_non_array_data_traps(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import evaluate_eligibility

    packet = _rebuild_packet(_eligible_packet(tmp_path), "data-traps", {"findings": []})
    with pytest.raises(ValueError, match="JSON array"):
        evaluate_eligibility(packet, target_column="label")


def test_evaluate_eligibility_rejects_too_many_columns(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import MAX_COLUMNS, evaluate_eligibility

    packet = _eligible_packet(tmp_path)
    content = _module_json(packet, "dataset-profile")
    content["columns"] = [
        *content["columns"],
        *({**content["columns"][0], "name": f"extra-{index}"} for index in range(MAX_COLUMNS)),
    ]
    with pytest.raises(ValueError, match="column_count"):
        evaluate_eligibility(
            _rebuild_packet(packet, "dataset-profile", content), target_column="label"
        )


def test_evaluate_eligibility_rejects_too_many_target_classes(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import (
        MAX_TARGET_CLASSES,
        evaluate_eligibility,
    )

    packet = _eligible_packet(tmp_path)
    content = _module_json(packet, "target-profile")
    content["classes"] = [
        *content["classes"],
        *({**content["classes"][0], "value": f"c{index}"} for index in range(MAX_TARGET_CLASSES)),
    ]
    with pytest.raises(ValueError, match="target_distinct_non_null"):
        evaluate_eligibility(
            _rebuild_packet(packet, "target-profile", content), target_column="label"
        )


def test_freeze_case_writes_builder_bundle_and_metrics(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import (
        REVIEW_CLASSIFIER_TASK_PROMPT,
        freeze_case,
    )

    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    ticks = iter((1.0, 1.25))
    result = freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze",
        case_id="case-a",
        target_column="label",
        source_id="datascibench:example-a",
        clock=lambda: next(ticks),
    )
    freeze = tmp_path / "freeze"
    assert (freeze / "packet.json").is_file()
    assert (freeze / "build-record.json").is_file()
    assert (freeze / "case.json").is_file()
    assert (freeze / "packet-build-metrics.json").is_file()
    assert (freeze / "freeze-note.json").is_file()
    assert (freeze / "data.csv").is_file()
    assert result.packet_build_metrics.elapsed_seconds == 0.25
    assert result.packet_build_metrics.estimated_cost_usd == 0.0
    assert result.case.task_prompt == REVIEW_CLASSIFIER_TASK_PROMPT
    assert result.note.inheritance == "datasets_only"
    assert result.note.license_accepted is True
    assert "likely_identifier" in result.note.eligibility_signals


def test_freeze_case_refuses_existing_output_and_missing_signals(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import freeze_case

    existing = tmp_path / "freeze"
    existing.mkdir()
    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    with pytest.raises(FileExistsError):
        freeze_case(
            dataset_path=source,
            output=existing,
            case_id="case-a",
            target_column="label",
            source_id="src",
        )
    balanced = write_csv(
        tmp_path / "balanced.csv",
        [{"label": 0, "noise": 1}, {"label": 1, "noise": 1}],
    )
    dest = tmp_path / "rejected"
    with pytest.raises(ValueError, match="no eligibility signal"):
        freeze_case(
            dataset_path=balanced,
            output=dest,
            case_id="case-b",
            target_column="label",
            source_id="src",
        )
    assert not dest.exists()


def test_freeze_case_rejects_oversize_sources_before_build(tmp_path, monkeypatch) -> None:
    from dsx.experiments.data_access import realistic
    from dsx.experiments.data_access.realistic import (
        MAX_COLUMNS,
        MAX_ROWS,
        MAX_TARGET_CLASSES,
        freeze_case,
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("build_packet should not run")

    monkeypatch.setattr(realistic, "build_packet", boom)
    too_many_rows = write_csv(
        tmp_path / "rows.csv",
        [{"label": 0, "noise": 1} for _ in range(MAX_ROWS + 1)],
    )
    dest_rows = tmp_path / "too-many-rows"
    with pytest.raises(ValueError, match="row_count"):
        freeze_case(
            dataset_path=too_many_rows,
            output=dest_rows,
            case_id="case-r",
            target_column="label",
            source_id="src",
        )
    assert not dest_rows.exists()

    wide = write_csv(
        tmp_path / "wide.csv",
        [{"label": 0, **{f"c{index}": index for index in range(MAX_COLUMNS)}}],
    )
    dest_cols = tmp_path / "too-many-cols"
    with pytest.raises(ValueError, match="column_count"):
        freeze_case(
            dataset_path=wide,
            output=dest_cols,
            case_id="case-c",
            target_column="label",
            source_id="src",
        )
    assert not dest_cols.exists()

    many_classes = write_csv(
        tmp_path / "classes.csv",
        [{"label": index, "noise": 1} for index in range(MAX_TARGET_CLASSES + 1)],
    )
    dest_classes = tmp_path / "too-many-classes"
    with pytest.raises(ValueError, match="target_distinct_non_null"):
        freeze_case(
            dataset_path=many_classes,
            output=dest_classes,
            case_id="case-t",
            target_column="label",
            source_id="src",
        )
    assert not dest_classes.exists()
