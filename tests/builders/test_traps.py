"""Tests for warning-only data-trap detectors."""

from __future__ import annotations

import pytest

from dsx.builders.models import ClassCount, TargetDistribution
from dsx.builders.traps import (
    detect_augmentation_before_split,
    detect_augmentation_changed_target_distribution,
    detect_augmentation_on_evaluation,
    detect_target_class_imbalance,
)
from dsx.pipeline import SnapshotRole, TransformationGraph, TransformationOperation
from tests.builders.helpers import manifest, snapshot, step


def _distribution(
    snapshot_id: str,
    classes: tuple[tuple[object, int], ...],
    *,
    null_count: int = 0,
) -> TargetDistribution:
    non_null = sum(count for _, count in classes)
    return TargetDistribution(
        snapshot_id=snapshot_id,
        null_count=null_count,
        non_null_count=non_null,
        classes=tuple(
            ClassCount(
                value=value,
                count=count,
                rate=(count / non_null) if non_null else 0.0,
            )
            for value, count in classes
        ),
        majority_class_rate=(max((count for _, count in classes), default=0) / non_null)
        if non_null
        else 0.0,
    )


def test_imbalance_for_zero_one_and_threshold() -> None:
    evidence = ("sha256:" + "a" * 64,)
    zero = detect_target_class_imbalance(
        _distribution("current", ()), evidence_refs=evidence
    )
    one = detect_target_class_imbalance(
        _distribution("current", (("pos", 10),)), evidence_refs=evidence
    )
    below = detect_target_class_imbalance(
        _distribution("current", (("pos", 1), ("neg", 6))), evidence_refs=evidence
    )
    at_threshold = detect_target_class_imbalance(
        _distribution("current", (("pos", 1), ("neg", 5))), evidence_refs=evidence
    )
    above = detect_target_class_imbalance(
        _distribution("current", (("pos", 2), ("neg", 5))), evidence_refs=evidence
    )
    assert zero is not None and zero.details["observed_class_count"] == 0
    assert one is not None and one.kind == "target_class_imbalance"
    assert below is not None
    assert at_threshold is None
    assert above is None
    assert zero.evidence_refs == evidence
    assert zero.snapshot_ids == ("current",)


def test_augmentation_before_split_only_when_upstream_of_split() -> None:
    before = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("aug", "b" * 64, SnapshotRole.intermediate),
                snapshot("train", "c" * 64, SnapshotRole.train),
                snapshot("test", "d" * 64, SnapshotRole.test),
            ),
            steps=(
                step("oversample", TransformationOperation.augment, ("raw",), ("aug",)),
                step("split", TransformationOperation.split, ("aug",), ("train", "test")),
            ),
        )
    )
    after = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("natural", "b" * 64, SnapshotRole.train),
                snapshot("test", "c" * 64, SnapshotRole.test),
                snapshot("train", "d" * 64, SnapshotRole.train),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("natural", "test")),
                step("oversample", TransformationOperation.augment, ("natural",), ("train",)),
            ),
        )
    )
    evidence = ("manifest:" + "e" * 64,)
    fired = detect_augmentation_before_split(
        before, current_snapshot_id="train", evidence_refs=evidence
    )
    skipped = detect_augmentation_before_split(
        after, current_snapshot_id="train", evidence_refs=evidence
    )
    assert len(fired) == 1
    assert fired[0].kind == "augmentation_before_split"
    assert fired[0].step_ids == ("oversample", "split")
    assert fired[0].evidence_refs == evidence
    assert skipped == ()


def test_augmentation_on_evaluation_for_val_and_test_lineage() -> None:
    graph = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("aug", "b" * 64, SnapshotRole.intermediate),
                snapshot("train", "c" * 64, SnapshotRole.train),
                snapshot("valid", "d" * 64, SnapshotRole.validation),
            ),
            steps=(
                step("oversample", TransformationOperation.augment, ("raw",), ("aug",)),
                step("split", TransformationOperation.split, ("aug",), ("train", "valid")),
            ),
        )
    )
    findings = detect_augmentation_on_evaluation(graph, evidence_refs=("manifest:" + "e" * 64,))
    assert len(findings) == 1
    assert findings[0].kind == "augmentation_on_evaluation"
    assert "valid" in findings[0].snapshot_ids
    assert findings[0].step_ids == ("oversample",)

    train_only = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("natural", "b" * 64, SnapshotRole.train),
                snapshot("valid", "c" * 64, SnapshotRole.validation),
                snapshot("train", "d" * 64, SnapshotRole.train),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("natural", "valid")),
                step("oversample", TransformationOperation.augment, ("natural",), ("train",)),
            ),
        )
    )
    train_only_traps = detect_augmentation_on_evaluation(
        train_only, evidence_refs=("manifest:" + "e" * 64,)
    )
    assert train_only_traps == ()

    direct = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("train", "b" * 64, SnapshotRole.train),
                snapshot("valid", "c" * 64, SnapshotRole.validation),
            ),
            steps=(
                step("copy", TransformationOperation.filter, ("raw",), ("train",)),
                step("oversample", TransformationOperation.augment, ("raw",), ("valid",)),
            ),
        )
    )
    findings = detect_augmentation_on_evaluation(direct, evidence_refs=("manifest:" + "e" * 64,))
    assert len(findings) == 1
    assert findings[0].snapshot_ids == ("valid",)

    side_split = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("train", "b" * 64, SnapshotRole.train),
                snapshot("hold", "c" * 64, SnapshotRole.test),
            ),
            steps=(
                step("oversample", TransformationOperation.augment, ("raw",), ("train",)),
                step("split", TransformationOperation.split, ("raw",), ("hold",)),
            ),
        )
    )
    assert (
        detect_augmentation_before_split(
            side_split, current_snapshot_id="train", evidence_refs=("manifest:" + "e" * 64,)
        )
        == ()
    )


def test_distribution_change_threshold_union_and_skip_rules() -> None:
    graph = TransformationGraph.from_manifest(
        manifest(
            current="after",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("before", "b" * 64, SnapshotRole.train),
                snapshot("after", "c" * 64, SnapshotRole.train),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("before",)),
                step("oversample", TransformationOperation.augment, ("before",), ("after",)),
            ),
        )
    )
    before = _distribution("before", (("pos", 19), ("neg", 1)))
    after_exact = _distribution("after", (("pos", 19), ("neg", 2)))
    # 19/20=0.95 vs 19/21≈0.905, 1/20=0.05 vs 2/21≈0.095 -> max change ≈ 0.045 < 0.05
    below = detect_augmentation_changed_target_distribution(
        graph=graph,
        current_snapshot_id="after",
        distributions={"before": before, "after": after_exact},
        evidence_refs=("sha256:" + "a" * 64,),
    )
    at_threshold = detect_augmentation_changed_target_distribution(
        graph=graph,
        current_snapshot_id="after",
        distributions={
            "before": _distribution("before", (("pos", 10), ("neg", 10))),
            "after": _distribution("after", (("pos", 11), ("neg", 9))),
        },
        evidence_refs=("sha256:" + "a" * 64,),
    )
    union = detect_augmentation_changed_target_distribution(
        graph=graph,
        current_snapshot_id="after",
        distributions={
            "before": _distribution("before", (("pos", 20),)),
            "after": _distribution("after", (("pos", 10), ("neg", 10))),
        },
        evidence_refs=("sha256:" + "a" * 64, "manifest:" + "e" * 64),
    )
    skipped_missing = detect_augmentation_changed_target_distribution(
        graph=graph,
        current_snapshot_id="after",
        distributions={"before": before},
        evidence_refs=("sha256:" + "a" * 64,),
    )
    multi = TransformationGraph.from_manifest(
        manifest(
            current="after",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("left", "b" * 64, SnapshotRole.intermediate),
                snapshot("right", "c" * 64, SnapshotRole.intermediate),
                snapshot("after", "d" * 64, SnapshotRole.train),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("left", "right")),
                step(
                    "oversample",
                    TransformationOperation.augment,
                    ("left", "right"),
                    ("after",),
                ),
            ),
        )
    )
    skipped_multi = detect_augmentation_changed_target_distribution(
        graph=multi,
        current_snapshot_id="after",
        distributions={
            "left": before,
            "right": before,
            "after": _distribution("after", (("pos", 1), ("neg", 1))),
        },
        evidence_refs=("sha256:" + "a" * 64,),
    )
    assert below == ()
    assert len(at_threshold) == 1
    assert at_threshold[0].kind == "augmentation_changed_target_distribution"
    assert at_threshold[0].details["maximum_absolute_rate_change"] == pytest.approx(0.05)
    assert len(union) == 1
    assert "neg" in [
        item["value"] for item in union[0].details["class_rate_changes"]  # type: ignore[index]
    ]
    assert f"sha256:{'b' * 64}" in union[0].evidence_refs
    assert f"sha256:{'c' * 64}" in union[0].evidence_refs
    assert f"manifest:{'e' * 64}" in union[0].evidence_refs
    assert skipped_missing == ()
    assert skipped_multi == ()
