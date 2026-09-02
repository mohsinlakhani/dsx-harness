"""Tests for packet assembly, history, and build records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsx.builders import PacketBuildRequest, build_packet
from dsx.builders.models import (
    CURRENT_SNAPSHOT_ID,
    DATA_TRAPS_MODULE_ID,
    DATASET_PROFILE_MODULE_ID,
    TARGET_PROFILE_MODULE_ID,
    TRANSFORMATION_HISTORY_MODULE_ID,
    DataTrap,
    PacketBuildRecord,
)
from dsx.pipeline import DatasetFormat, SnapshotRole, TransformationOperation
from tests.builders.helpers import digest_of, manifest, snapshot, step, write_csv, write_parquet


def _rows(labels: list[object], extra: str = "x") -> list[dict[str, object]]:
    return [{"label": label, "feature": extra} for label in labels]


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
        TARGET_PROFILE_MODULE_ID,
        DATA_TRAPS_MODULE_ID,
    ]
    assert first.packet.digest() == second.packet.digest()
    assert first.packet.canonical_json() == second.packet.canonical_json()
    target = first.packet.modules[1].content
    assert isinstance(target, dict)
    assert target["current_snapshot_id"] == CURRENT_SNAPSHOT_ID
    assert target["null_count"] == 1
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
            snapshot("holdout", "d" * 64, SnapshotRole.test, path="absent.csv", fmt=DatasetFormat.csv),
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
    history = next(
        module for module in result.packet.modules if module.module_id == TRANSFORMATION_HISTORY_MODULE_ID
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
    assert resolve_snapshot_path("rel.csv", None).name == "rel.csv"
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
            snapshot("raw", digest_of(raw), SnapshotRole.source, path="raw.csv", fmt=DatasetFormat.csv),
            snapshot("ghost", "b" * 64, SnapshotRole.intermediate),
            snapshot(
                "current",
                digest_of(current),
                SnapshotRole.train,
                path="current.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot("side", digest_of(side), SnapshotRole.intermediate, path="side.csv", fmt=DatasetFormat.csv),
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
            snapshot("raw", digest_of(raw), SnapshotRole.source, path="raw.csv", fmt=DatasetFormat.csv),
            snapshot("side", digest_of(side), SnapshotRole.intermediate, path="side.csv", fmt=DatasetFormat.csv),
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
    assert multi_result.packet.modules[1].content["augmentation_distributions"] == []  # type: ignore[index]
