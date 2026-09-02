"""Tests for packet assembly, history, and build records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsx.builders import (
    PacketBuildRequest,
    build_packet,
    load_previous_bundle,
    write_packet_bundle,
)
from dsx.builders.models import (
    COLUMN_PROFILE_MODULE_ID,
    COLUMN_PROFILE_SCHEMA,
    COLUMN_PROFILE_TYPE,
    CURRENT_SNAPSHOT_ID,
    DATA_TRAPS_MODULE_ID,
    DATA_TRAPS_SCHEMA,
    DATA_TRAPS_TYPE,
    DATASET_PROFILE_MODULE_ID,
    DATASET_PROFILE_SCHEMA,
    DATASET_PROFILE_TYPE,
    FEATURE_RISKS_MODULE_ID,
    FEATURE_RISKS_SCHEMA,
    FEATURE_RISKS_TYPE,
    TARGET_PROFILE_MODULE_ID,
    TARGET_PROFILE_SCHEMA,
    TARGET_PROFILE_TYPE,
    TRANSFORMATION_HISTORY_MODULE_ID,
    TRANSFORMATION_HISTORY_SCHEMA,
    ColumnsProfile,
    DataTrap,
    FeatureRisks,
    PacketBuildRecord,
)
from dsx.packet.models import DatasetRef, DsxPacket, PacketModule
from dsx.pipeline import DatasetFormat, SnapshotRole, TransformationOperation
from tests.builders.helpers import digest_of, manifest, snapshot, step, write_csv, write_parquet


def _rows(labels: list[object], extra: str = "x") -> list[dict[str, object]]:
    return [{"label": label, "feature": extra} for label in labels]


def _cardinality_rows() -> list[dict[str, object]]:
    return [
        {
            "row_id": 1,
            "duplicate_value": "a",
            "nullable_unique": "x",
            "constant_value": "k",
            "all_null": None,
            "label": "pos",
        },
        {
            "row_id": 2,
            "duplicate_value": "a",
            "nullable_unique": "y",
            "constant_value": "k",
            "all_null": None,
            "label": "neg",
        },
        {
            "row_id": 3,
            "duplicate_value": "b",
            "nullable_unique": None,
            "constant_value": "k",
            "all_null": None,
            "label": "other",
        },
    ]


def _module(packet: DsxPacket, module_id: str) -> PacketModule:
    return next(module for module in packet.modules if module.module_id == module_id)


def test_build_without_manifest_omits_history_and_is_deterministic(tmp_path: Path) -> None:
    dataset = write_csv(tmp_path / "current.csv", _rows(["pos", "neg", "pos", None]))
    request = PacketBuildRequest(
        dataset_path=dataset,
        target_column="label",
        packet_id="case-v1",
    )
    first = build_packet(request)
    second = build_packet(request)
    assert [module.module_id for module in first.packet.modules] == [
        DATASET_PROFILE_MODULE_ID,
        COLUMN_PROFILE_MODULE_ID,
        TARGET_PROFILE_MODULE_ID,
        DATA_TRAPS_MODULE_ID,
        FEATURE_RISKS_MODULE_ID,
    ]
    assert [
        (item.module_id, item.module_type, item.schema_version)
        for item in first.build_record.modules
    ] == [
        (DATASET_PROFILE_MODULE_ID, DATASET_PROFILE_TYPE, DATASET_PROFILE_SCHEMA),
        (COLUMN_PROFILE_MODULE_ID, COLUMN_PROFILE_TYPE, COLUMN_PROFILE_SCHEMA),
        (TARGET_PROFILE_MODULE_ID, TARGET_PROFILE_TYPE, TARGET_PROFILE_SCHEMA),
        (DATA_TRAPS_MODULE_ID, DATA_TRAPS_TYPE, DATA_TRAPS_SCHEMA),
        (FEATURE_RISKS_MODULE_ID, FEATURE_RISKS_TYPE, FEATURE_RISKS_SCHEMA),
    ]
    assert first.packet.digest() == second.packet.digest()
    assert first.packet.canonical_json() == second.packet.canonical_json()
    target = _module(first.packet, TARGET_PROFILE_MODULE_ID).content
    assert isinstance(target, dict)
    assert target["current_snapshot_id"] == CURRENT_SNAPSHOT_ID
    assert target["null_count"] == 1
    risks = _module(first.packet, FEATURE_RISKS_MODULE_ID).content
    assert isinstance(risks, dict)
    assert risks["findings"] == []
    assert first.build_record.revision == 1
    assert first.build_record.previous_packet_digest is None
    assert first.manifest is None


def test_history_lineage_unavailable_snapshots_and_digest_checks(tmp_path: Path) -> None:
    raw = write_csv(tmp_path / "raw.csv", _rows(["neg"] * 9 + ["pos"]))
    natural = write_csv(tmp_path / "train-natural.csv", _rows(["neg"] * 7 + ["pos"]))
    balanced = write_csv(
        tmp_path / "train-balanced.csv", _rows(["neg"] * 7 + ["pos"] * 7)
    )
    missing_history = tmp_path / "absent.csv"
    declared = manifest(
        current="train-balanced",
        snapshots=(
            snapshot(
                "raw",
                digest_of(raw),
                SnapshotRole.source,
                path="raw.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "train-natural",
                digest_of(natural),
                SnapshotRole.train,
                path="train-natural.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "train-balanced",
                digest_of(balanced),
                SnapshotRole.train,
                path="train-balanced.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "holdout",
                "d" * 64,
                SnapshotRole.test,
                path="absent.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot("omitted", "e" * 64, SnapshotRole.intermediate),
        ),
        steps=(
            step(
                "split",
                TransformationOperation.split,
                ("raw",),
                ("train-natural", "holdout"),
            ),
            step(
                "oversample",
                TransformationOperation.augment,
                ("train-natural",),
                ("train-balanced",),
            ),
            step(
                "unused",
                TransformationOperation.filter,
                ("raw",),
                ("omitted",),
            ),
        ),
    )
    result = build_packet(
        PacketBuildRequest(
            dataset_path=balanced,
            target_column="label",
            packet_id="fraud-v1",
            manifest=declared,
            snapshot_root=tmp_path,
        )
    )
    assert [module.module_id for module in result.packet.modules] == [
        DATASET_PROFILE_MODULE_ID,
        COLUMN_PROFILE_MODULE_ID,
        TARGET_PROFILE_MODULE_ID,
        TRANSFORMATION_HISTORY_MODULE_ID,
        DATA_TRAPS_MODULE_ID,
        FEATURE_RISKS_MODULE_ID,
    ]
    assert [
        (item.module_id, item.schema_version) for item in result.build_record.modules
    ] == [
        (DATASET_PROFILE_MODULE_ID, DATASET_PROFILE_SCHEMA),
        (COLUMN_PROFILE_MODULE_ID, COLUMN_PROFILE_SCHEMA),
        (TARGET_PROFILE_MODULE_ID, TARGET_PROFILE_SCHEMA),
        (TRANSFORMATION_HISTORY_MODULE_ID, TRANSFORMATION_HISTORY_SCHEMA),
        (DATA_TRAPS_MODULE_ID, DATA_TRAPS_SCHEMA),
        (FEATURE_RISKS_MODULE_ID, FEATURE_RISKS_SCHEMA),
    ]
    history = next(
        module
        for module in result.packet.modules
        if module.module_id == TRANSFORMATION_HISTORY_MODULE_ID
    )
    content = history.content
    assert isinstance(content, dict)
    assert content["steps"][0]["step_id"] == "split"
    assert content["steps"][1]["step_id"] == "oversample"
    assert "unused" not in [item["step_id"] for item in content["steps"]]
    assert "holdout" in content["unavailable_snapshot_ids"]
    assert "omitted" in content["unavailable_snapshot_ids"]
    assert "train-natural" in content["accessible_snapshot_ids"]
    target = next(
        module for module in result.packet.modules if module.module_id == TARGET_PROFILE_MODULE_ID
    )
    assert isinstance(target.content, dict)
    distributions = target.content["augmentation_distributions"]
    assert len(distributions) == 1
    assert distributions[0]["step_id"] == "oversample"
    traps = next(
        module for module in result.packet.modules if module.module_id == DATA_TRAPS_MODULE_ID
    )
    kinds = {item["kind"] for item in traps.content}  # type: ignore[index]
    assert "augmentation_changed_target_distribution" in kinds
    assert "augmentation_before_split" not in kinds
    second_history = build_packet(
        PacketBuildRequest(
            dataset_path=balanced,
            target_column="label",
            packet_id="fraud-v1",
            manifest=declared,
            snapshot_root=tmp_path,
        )
    )
    assert result.packet.canonical_json() == second_history.packet.canonical_json()
    assert result.build_record.manifest_digest == declared.digest()
    assert result.build_record.revision == 1
    assert result.build_record.previous_packet_digest is None
    assert result.build_record.dataset_digest == digest_of(balanced)
    dist_trap = next(
        item
        for item in traps.content  # type: ignore[union-attr]
        if item["kind"] == "augmentation_changed_target_distribution"
    )
    assert f"sha256:{digest_of(natural)}" in dist_trap["evidence_refs"]
    assert f"sha256:{digest_of(balanced)}" in dist_trap["evidence_refs"]
    assert f"manifest:{declared.digest()}" in dist_trap["evidence_refs"]

    mismatched = write_csv(tmp_path / "wrong.csv", _rows(["pos"]))
    with pytest.raises(ValueError, match="current dataset digest"):
        build_packet(
            PacketBuildRequest(
                dataset_path=mismatched,
                target_column="label",
                packet_id="fraud-v1",
                manifest=declared,
                snapshot_root=tmp_path,
            )
        )
    natural.write_text("label,feature\nneg,tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        build_packet(
            PacketBuildRequest(
                dataset_path=balanced,
                target_column="label",
                packet_id="fraud-v1",
                manifest=declared,
                snapshot_root=tmp_path,
            )
        )
    assert not missing_history.exists()


def test_parquet_current_dataset_and_previous_revision(tmp_path: Path) -> None:
    dataset = write_parquet(tmp_path / "current.parquet", _rows(["neg", "pos"]))
    first = build_packet(
        PacketBuildRequest(
            dataset_path=dataset,
            target_column="label",
            packet_id="case-v1",
        )
    )
    second = build_packet(
        PacketBuildRequest(
            dataset_path=dataset,
            target_column="label",
            packet_id="case-v2",
            previous_build_record=first.build_record,
            previous_packet_digest=first.packet.digest(),
        )
    )
    assert second.build_record.revision == 2
    assert second.build_record.previous_packet_digest == first.packet.digest()
    assert second.packet.packet_id == "case-v2"


def test_request_and_record_validation_errors() -> None:
    with pytest.raises(ValidationError, match="supplied together"):
        PacketBuildRequest(
            dataset_path=Path("x.csv"),
            target_column="label",
            packet_id="case-v1",
            previous_packet_digest="a" * 64,
        )
    record = PacketBuildRecord(
        build_id="build1",
        built_at=datetime.now(UTC),
        revision=1,
        packet_id="case-v1",
        packet_digest="a" * 64,
        dataset_path="x.csv",
        dataset_digest="b" * 64,
        target_column="label",
        modules=(
            {
                "module_id": "dataset-profile",
                "module_type": "profile.dataset",
                "schema_version": "profile.dataset/v1",
            },
        ),
    )
    with pytest.raises(ValidationError, match="does not match"):
        PacketBuildRequest(
            dataset_path=Path("x.csv"),
            target_column="label",
            packet_id="case-v1",
            previous_build_record=record,
            previous_packet_digest="c" * 64,
        )
    with pytest.raises(ValidationError, match="aware UTC"):
        PacketBuildRecord(
            build_id="build1",
            built_at=datetime(2026, 9, 2, 12, 0, 0),
            revision=1,
            packet_id="case-v1",
            packet_digest="a" * 64,
            dataset_path="x.csv",
            dataset_digest="b" * 64,
            target_column="label",
            modules=record.modules,
        )
    with pytest.raises(ValidationError, match="unique"):
        DataTrap(
            finding_id="dup",
            kind="target_class_imbalance",
            message="x",
            evidence_refs=("same", "same"),
        )
    with pytest.raises(ValidationError, match="unique"):
        DataTrap(
            finding_id="dup",
            kind="target_class_imbalance",
            message="x",
            snapshot_ids=("a", "a"),
        )
    with pytest.raises(ValidationError, match="unique"):
        DataTrap(
            finding_id="dup",
            kind="target_class_imbalance",
            message="x",
            step_ids=("a", "a"),
        )
    with pytest.raises(ValidationError, match="standard JSON"):
        DataTrap(
            finding_id="dup",
            kind="target_class_imbalance",
            message="x",
            details={"n": float("nan")},
        )
    assert record.digest() == PacketBuildRecord.model_validate_json(
        record.canonical_json()
    ).digest()


def test_build_covers_edge_paths(tmp_path: Path) -> None:
    from dsx.builders.build import resolve_snapshot_path

    assert resolve_snapshot_path("/abs/raw.csv", tmp_path) == Path("/abs/raw.csv")
    with pytest.raises(ValueError, match="requires snapshot_root"):
        resolve_snapshot_path("rel.csv", None)
    with pytest.raises(ValidationError, match="snapshot_root is required"):
        PacketBuildRequest(
            dataset_path=tmp_path / "current.csv",
            target_column="label",
            packet_id="case-v1",
            manifest=manifest(
                current="current",
                snapshots=(
                    snapshot(
                        "current",
                        "a" * 64,
                        SnapshotRole.source,
                        path="current.csv",
                        fmt=DatasetFormat.csv,
                    ),
                ),
            ),
        )
    with pytest.raises(ValueError, match="does not exist"):
        build_packet(
            PacketBuildRequest(
                dataset_path=tmp_path / "missing.csv",
                target_column="label",
                packet_id="case-v1",
            )
        )

    raw = write_csv(tmp_path / "raw.csv", _rows(["neg"] * 8 + ["pos"]))
    current = write_csv(tmp_path / "current.csv", _rows(["pos"] * 10))
    side = write_csv(tmp_path / "side.csv", _rows(["neg"]))
    no_target = write_csv(tmp_path / "features.csv", [{"feature": "x"}])
    declared = manifest(
        current="current",
        snapshots=(
            snapshot(
                "raw",
                digest_of(raw),
                SnapshotRole.source,
                path="raw.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot("ghost", "b" * 64, SnapshotRole.intermediate),
            snapshot(
                "current",
                digest_of(current),
                SnapshotRole.train,
                path="current.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "side",
                digest_of(side),
                SnapshotRole.intermediate,
                path="side.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "features",
                digest_of(no_target),
                SnapshotRole.intermediate,
                path="features.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot("merged", "c" * 64, SnapshotRole.train),
        ),
        steps=(
            step("ghost-prep", TransformationOperation.filter, ("raw",), ("ghost",)),
            step("ghost-aug", TransformationOperation.augment, ("ghost",), ("features",)),
            step("oversample", TransformationOperation.augment, ("features",), ("current",)),
            step("side-aug", TransformationOperation.augment, ("raw",), ("side",)),
            step("multi", TransformationOperation.augment, ("side", "features"), ("merged",)),
        ),
    )
    result = build_packet(
        PacketBuildRequest(
            dataset_path=current,
            target_column="label",
            packet_id="edges-v1",
            manifest=declared,
            snapshot_root=tmp_path,
        )
    )
    kinds = {
        item["kind"]
        for module in result.packet.modules
        if module.module_id == DATA_TRAPS_MODULE_ID
        for item in module.content  # type: ignore[union-attr]
    }
    assert "target_class_imbalance" in kinds
    history = next(
        module.content
        for module in result.packet.modules
        if module.module_id == TRANSFORMATION_HISTORY_MODULE_ID
    )
    assert isinstance(history, dict)
    assert "side-aug" not in [item["step_id"] for item in history["steps"]]

    from dsx.builders.build import _unique

    assert _unique(("a", "b"), ("b", "c"), extra=("c", "d")) == ("a", "b", "c", "d")

    merged_current = write_csv(tmp_path / "merged.csv", _rows(["pos"] * 4 + ["neg"]))
    multi_manifest = manifest(
        current="merged",
        snapshots=(
            snapshot(
                "raw",
                digest_of(raw),
                SnapshotRole.source,
                path="raw.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "side",
                digest_of(side),
                SnapshotRole.intermediate,
                path="side.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "merged",
                digest_of(merged_current),
                SnapshotRole.train,
                path="merged.csv",
                fmt=DatasetFormat.csv,
            ),
        ),
        steps=(
            step("side-prep", TransformationOperation.filter, ("raw",), ("side",)),
            step("multi", TransformationOperation.augment, ("raw", "side"), ("merged",)),
        ),
    )
    multi_result = build_packet(
        PacketBuildRequest(
            dataset_path=merged_current,
            target_column="label",
            packet_id="multi-v1",
            manifest=multi_manifest,
            snapshot_root=tmp_path,
        )
    )
    target = _module(multi_result.packet, TARGET_PROFILE_MODULE_ID).content
    assert isinstance(target, dict)
    assert target["augmentation_distributions"] == []


def test_declared_format_empty_history_and_absolute_paths(tmp_path: Path) -> None:
    raw_rows = _rows(["neg", "pos"])
    raw_csv = write_csv(tmp_path / "raw.csv", raw_rows)
    raw_dat = tmp_path / "raw.dat"
    raw_dat.write_bytes(raw_csv.read_bytes())
    empty = write_csv(tmp_path / "empty.csv", [])
    nulls = write_csv(tmp_path / "nulls.csv", [{"label": None, "feature": "x"}] * 2)
    parquet_raw = write_parquet(tmp_path / "raw.parquet", raw_rows)
    current = write_csv(tmp_path / "current.csv", _rows(["pos"] * 4 + ["neg"] * 4))
    declared = manifest(
        current="current",
        snapshots=(
            snapshot(
                "raw",
                digest_of(raw_dat),
                SnapshotRole.source,
                path="raw.dat",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "empty",
                digest_of(empty),
                SnapshotRole.intermediate,
                path="empty.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "nulls",
                digest_of(nulls),
                SnapshotRole.intermediate,
                path="nulls.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "current",
                digest_of(current),
                SnapshotRole.train,
                path="current.csv",
                fmt=DatasetFormat.csv,
            ),
        ),
        steps=(
            step("to-empty", TransformationOperation.filter, ("raw",), ("empty",)),
            step("to-nulls", TransformationOperation.filter, ("empty",), ("nulls",)),
            step("oversample", TransformationOperation.augment, ("nulls",), ("current",)),
        ),
    )
    result = build_packet(
        PacketBuildRequest(
            dataset_path=current,
            target_column="label",
            packet_id="format-v1",
            manifest=declared,
            snapshot_root=tmp_path,
        )
    )
    history = next(
        module.content
        for module in result.packet.modules
        if module.module_id == TRANSFORMATION_HISTORY_MODULE_ID
    )
    assert isinstance(history, dict)
    assert "raw" in history["accessible_snapshot_ids"]
    assert "empty" in history["unavailable_snapshot_ids"]
    assert "nulls" in history["accessible_snapshot_ids"]
    traps = next(
        module.content
        for module in result.packet.modules
        if module.module_id == DATA_TRAPS_MODULE_ID
    )
    assert "augmentation_changed_target_distribution" not in {
        item["kind"] for item in traps  # type: ignore[union-attr]
    }

    parquet_current = write_parquet(tmp_path / "current.parquet", _rows(["pos", "neg"]))
    parquet_manifest = manifest(
        current="current",
        snapshots=(
            snapshot(
                "raw",
                digest_of(parquet_raw),
                SnapshotRole.source,
                path=str(parquet_raw.resolve()),
                fmt=DatasetFormat.parquet,
            ),
            snapshot(
                "current",
                digest_of(parquet_current),
                SnapshotRole.train,
                path=str(parquet_current.resolve()),
                fmt=DatasetFormat.parquet,
            ),
        ),
        steps=(step("copy", TransformationOperation.filter, ("raw",), ("current",)),),
    )
    absolute = build_packet(
        PacketBuildRequest(
            dataset_path=parquet_current,
            target_column="label",
            packet_id="abs-v1",
            manifest=parquet_manifest,
        )
    )
    abs_history = _module(absolute.packet, TRANSFORMATION_HISTORY_MODULE_ID).content
    assert isinstance(abs_history, dict)
    assert "raw" in abs_history["accessible_snapshot_ids"]

    pathless = write_csv(tmp_path / "pathless.csv", _rows(["pos", "neg"]))
    pathless_manifest = manifest(
        current="current",
        snapshots=(
            snapshot("current", digest_of(pathless), SnapshotRole.source),
        ),
    )
    pathless_result = build_packet(
        PacketBuildRequest(
            dataset_path=pathless,
            target_column="label",
            packet_id="pathless-v1",
            manifest=pathless_manifest,
        )
    )
    assert pathless_result.build_record.manifest_digest == pathless_manifest.digest()


def test_column_profile_and_feature_risks_payloads(tmp_path: Path) -> None:
    dataset = write_csv(tmp_path / "current.csv", _cardinality_rows())
    result = build_packet(
        PacketBuildRequest(
            dataset_path=dataset,
            target_column="label",
            packet_id="ids-v1",
        )
    )
    evidence = (f"sha256:{result.build_record.dataset_digest}",)
    columns_module = _module(result.packet, COLUMN_PROFILE_MODULE_ID)
    risks_module = _module(result.packet, FEATURE_RISKS_MODULE_ID)
    assert columns_module.schema_version == COLUMN_PROFILE_SCHEMA
    assert risks_module.schema_version == FEATURE_RISKS_SCHEMA
    assert columns_module.evidence_refs == evidence
    assert risks_module.evidence_refs == evidence
    columns_content = columns_module.content
    risks_content = risks_module.content
    assert isinstance(columns_content, dict)
    assert isinstance(risks_content, dict)
    assert not isinstance(columns_content, list)
    profile = ColumnsProfile.model_validate(columns_content)
    risks = FeatureRisks.model_validate(risks_content)
    assert [column.name for column in profile.columns] == [
        "row_id",
        "duplicate_value",
        "nullable_unique",
        "constant_value",
        "all_null",
        "label",
    ]
    by_name = {column.name: column for column in profile.columns}
    assert by_name["row_id"].non_null_count == 3
    assert by_name["row_id"].distinct_count == 3
    assert by_name["row_id"].uniqueness_rate == 1.0
    assert by_name["duplicate_value"].non_null_count == 3
    assert by_name["duplicate_value"].distinct_count == 2
    assert by_name["duplicate_value"].uniqueness_rate == 2 / 3
    assert by_name["nullable_unique"].non_null_count == 2
    assert by_name["nullable_unique"].distinct_count == 2
    assert by_name["nullable_unique"].uniqueness_rate == 2 / 3
    assert by_name["constant_value"].distinct_count == 1
    assert by_name["all_null"].non_null_count == 0
    assert by_name["all_null"].distinct_count == 0
    assert [finding.column for finding in risks.findings] == ["row_id"]
    assert risks.findings[0].finding_id == "likely-identifier:row_id"
    assert risks.findings[0].evidence_refs == evidence
    assert risks.findings[0].recommended_action == "exclude_or_verify"

    output = tmp_path / "bundle"
    write_packet_bundle(result, output)
    restored = DsxPacket.model_validate_json(
        (output / "packet.json").read_text(encoding="utf-8")
    )
    record = PacketBuildRecord.model_validate_json(
        (output / "build-record.json").read_text(encoding="utf-8")
    )
    assert restored.digest() == result.packet.digest() == record.packet_digest
    assert not any(path.suffix in {".csv", ".parquet"} for path in output.iterdir())
    ColumnsProfile.model_validate(_module(restored, COLUMN_PROFILE_MODULE_ID).content)
    FeatureRisks.model_validate(_module(restored, FEATURE_RISKS_MODULE_ID).content)


def test_legacy_predecessor_without_new_modules_is_accepted(tmp_path: Path) -> None:
    dataset = write_csv(tmp_path / "current.csv", _rows(["pos", "neg"]))
    digest = digest_of(dataset)
    evidence = (f"sha256:{digest}",)
    legacy = DsxPacket(
        packet_id="legacy-v1",
        dataset=DatasetRef(digest=digest, name=dataset.name),
        modules=(
            PacketModule(
                module_id=DATASET_PROFILE_MODULE_ID,
                module_type=DATASET_PROFILE_TYPE,
                schema_version=DATASET_PROFILE_SCHEMA,
                content={
                    "current_snapshot_id": CURRENT_SNAPSHOT_ID,
                    "row_count": 2,
                    "columns": [
                        {
                            "name": "label",
                            "duckdb_type": "VARCHAR",
                            "missing_count": 0,
                            "missing_rate": 0.0,
                        },
                        {
                            "name": "feature",
                            "duckdb_type": "VARCHAR",
                            "missing_count": 0,
                            "missing_rate": 0.0,
                        },
                    ],
                },
                evidence_refs=evidence,
            ),
            PacketModule(
                module_id=TARGET_PROFILE_MODULE_ID,
                module_type=TARGET_PROFILE_TYPE,
                schema_version=TARGET_PROFILE_SCHEMA,
                content={
                    "target_column": "label",
                    "current_snapshot_id": CURRENT_SNAPSHOT_ID,
                    "null_count": 0,
                    "non_null_count": 2,
                    "classes": [
                        {"value": "neg", "count": 1, "rate": 0.5},
                        {"value": "pos", "count": 1, "rate": 0.5},
                    ],
                    "majority_class_rate": 0.5,
                    "augmentation_distributions": [],
                },
                evidence_refs=evidence,
            ),
            PacketModule(
                module_id=DATA_TRAPS_MODULE_ID,
                module_type=DATA_TRAPS_TYPE,
                schema_version=DATA_TRAPS_SCHEMA,
                content=[],
                evidence_refs=evidence,
            ),
        ),
    )
    record = PacketBuildRecord(
        build_id="legacy-build",
        built_at=datetime.now(UTC),
        revision=1,
        packet_id="legacy-v1",
        packet_digest=legacy.digest(),
        dataset_path=str(dataset),
        dataset_digest=digest,
        target_column="label",
        modules=(
            {
                "module_id": DATASET_PROFILE_MODULE_ID,
                "module_type": DATASET_PROFILE_TYPE,
                "schema_version": DATASET_PROFILE_SCHEMA,
            },
            {
                "module_id": TARGET_PROFILE_MODULE_ID,
                "module_type": TARGET_PROFILE_TYPE,
                "schema_version": TARGET_PROFILE_SCHEMA,
            },
            {
                "module_id": DATA_TRAPS_MODULE_ID,
                "module_type": DATA_TRAPS_TYPE,
                "schema_version": DATA_TRAPS_SCHEMA,
            },
        ),
    )
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "packet.json").write_text(legacy.canonical_json() + "\n", encoding="utf-8")
    (previous / "build-record.json").write_text(
        record.canonical_json() + "\n", encoding="utf-8"
    )
    loaded_packet, loaded_record = load_previous_bundle(previous)
    assert COLUMN_PROFILE_MODULE_ID not in [
        module.module_id for module in loaded_packet.modules
    ]
    assert FEATURE_RISKS_MODULE_ID not in [
        module.module_id for module in loaded_packet.modules
    ]
    result = build_packet(
        PacketBuildRequest(
            dataset_path=dataset,
            target_column="label",
            packet_id="case-v2",
            previous_build_record=loaded_record,
            previous_packet_digest=loaded_packet.digest(),
        )
    )
    assert result.build_record.revision == 2
    assert result.build_record.previous_packet_digest == legacy.digest()
    assert [module.module_id for module in result.packet.modules] == [
        DATASET_PROFILE_MODULE_ID,
        COLUMN_PROFILE_MODULE_ID,
        TARGET_PROFILE_MODULE_ID,
        DATA_TRAPS_MODULE_ID,
        FEATURE_RISKS_MODULE_ID,
    ]


def test_profiling_failure_leaves_no_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = write_csv(tmp_path / "current.csv", _rows(["pos", "neg"]))
    output = tmp_path / "bundle"

    def fail_profile(*args: object, **kwargs: object) -> object:
        raise ValueError(f"could not parse csv dataset: {dataset}")

    monkeypatch.setattr("dsx.builders.build.profile_table_and_columns", fail_profile)
    with pytest.raises(ValueError, match="could not parse"):
        build_packet(
            PacketBuildRequest(
                dataset_path=dataset,
                target_column="label",
                packet_id="case-v1",
            )
        )
    assert not output.exists()
    assert list(tmp_path.glob(".bundle.tmp-*")) == []
