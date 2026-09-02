"""Deterministic warning-only data-trap detectors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from dsx.pipeline import TransformationGraph

from .models import DataTrap, TargetDistribution
from .profiling import class_sort_key, rates_by_class

IMBALANCE_RATIO_THRESHOLD = 0.20
DISTRIBUTION_CHANGE_THRESHOLD = 0.05


def detect_target_class_imbalance(
    distribution: TargetDistribution,
    *,
    evidence_refs: Sequence[str],
) -> DataTrap | None:
    classes = distribution.classes
    if not classes:
        return DataTrap(
            finding_id="target-class-imbalance",
            kind="target_class_imbalance",
            message="Target has no non-null class values.",
            snapshot_ids=(distribution.snapshot_id,),
            details={
                "observed_class_count": 0,
                "minimum_class_count": 0,
                "maximum_class_count": 0,
            },
            evidence_refs=tuple(evidence_refs),
        )
    counts = tuple(item.count for item in classes)
    minimum = min(counts)
    maximum = max(counts)
    if len(classes) == 1 or (maximum > 0 and minimum / maximum < IMBALANCE_RATIO_THRESHOLD):
        return DataTrap(
            finding_id="target-class-imbalance",
            kind="target_class_imbalance",
            message="Target class counts are imbalanced.",
            snapshot_ids=(distribution.snapshot_id,),
            details={
                "observed_class_count": len(classes),
                "minimum_class_count": minimum,
                "maximum_class_count": maximum,
                "ratio": None if maximum == 0 else minimum / maximum,
            },
            evidence_refs=tuple(evidence_refs),
        )
    return None


def detect_augmentation_before_split(
    graph: TransformationGraph,
    *,
    current_snapshot_id: str,
    evidence_refs: Sequence[str],
) -> tuple[DataTrap, ...]:
    _, lineage_steps = graph.lineage_to(current_snapshot_id)
    lineage = set(lineage_steps)
    findings: list[DataTrap] = []
    for augment_id in graph.augment_steps():
        if augment_id not in lineage:
            continue
        for split_id in graph.split_steps():
            if split_id not in lineage:
                continue
            if graph.step_precedes(augment_id, split_id):
                findings.append(
                    DataTrap(
                        finding_id=f"augmentation-before-split:{augment_id}:{split_id}",
                        kind="augmentation_before_split",
                        message="Augmentation occurs upstream of a split on the current lineage.",
                        snapshot_ids=tuple(
                            dict.fromkeys(
                                (
                                    *graph.step_inputs(augment_id),
                                    *graph.step_outputs(augment_id),
                                    *graph.step_inputs(split_id),
                                    *graph.step_outputs(split_id),
                                )
                            )
                        ),
                        step_ids=(augment_id, split_id),
                        details={"augment_step_id": augment_id, "split_step_id": split_id},
                        evidence_refs=tuple(evidence_refs),
                    )
                )
    return tuple(findings)


def detect_augmentation_on_evaluation(
    graph: TransformationGraph,
    *,
    evidence_refs: Sequence[str],
) -> tuple[DataTrap, ...]:
    findings: list[DataTrap] = []
    evaluation_ids = graph.evaluation_snapshot_ids()
    for augment_id in graph.augment_steps():
        related = [
            snapshot_id
            for snapshot_id in evaluation_ids
            if _step_reaches_snapshot(graph, augment_id, snapshot_id)
        ]
        if not related:
            continue
        findings.append(
            DataTrap(
                finding_id=f"augmentation-on-evaluation:{augment_id}",
                kind="augmentation_on_evaluation",
                message="Augmentation produces or is an ancestor of evaluation data.",
                snapshot_ids=tuple(related),
                step_ids=(augment_id,),
                details={"evaluation_snapshot_ids": list(related)},
                evidence_refs=tuple(evidence_refs),
            )
        )
    return tuple(findings)


def detect_augmentation_changed_target_distribution(
    *,
    graph: TransformationGraph,
    current_snapshot_id: str,
    distributions: Mapping[str, TargetDistribution],
    evidence_refs: Sequence[str],
) -> tuple[DataTrap, ...]:
    _, lineage_steps = graph.lineage_to(current_snapshot_id)
    findings: list[DataTrap] = []
    for step_id in graph.augment_steps():
        if step_id not in lineage_steps:
            continue
        inputs = graph.step_inputs(step_id)
        outputs = graph.step_outputs(step_id)
        if len(inputs) != 1 or len(outputs) != 1:
            continue
        before = distributions.get(inputs[0])
        after = distributions.get(outputs[0])
        if before is None or after is None:
            continue
        keys = set(rates_by_class(before)) | set(rates_by_class(after))
        before_rates = rates_by_class(before)
        after_rates = rates_by_class(after)
        changes = {key: abs(after_rates.get(key, 0.0) - before_rates.get(key, 0.0)) for key in keys}
        maximum = max(changes.values(), default=0.0)
        if maximum < DISTRIBUTION_CHANGE_THRESHOLD:
            continue
        snapshot_refs = (
            f"sha256:{graph.snapshot(inputs[0]).digest}",
            f"sha256:{graph.snapshot(outputs[0]).digest}",
        )
        findings.append(
            DataTrap(
                finding_id=f"augmentation-changed-target-distribution:{step_id}",
                kind="augmentation_changed_target_distribution",
                message="Augmentation changed the target class distribution.",
                snapshot_ids=(inputs[0], outputs[0]),
                step_ids=(step_id,),
                details={
                    "maximum_absolute_rate_change": maximum,
                    "threshold": DISTRIBUTION_CHANGE_THRESHOLD,
                    "class_rate_changes": [
                        {
                            "value": _value_for_key(key, before, after),
                            "before_rate": before_rates.get(key, 0.0),
                            "after_rate": after_rates.get(key, 0.0),
                        }
                        for key in sorted(keys)
                    ],
                },
                evidence_refs=tuple(dict.fromkeys((*snapshot_refs, *evidence_refs))),
            )
        )
    return tuple(findings)


def _step_reaches_snapshot(
    graph: TransformationGraph, step_id: str, snapshot_id: str
) -> bool:
    if snapshot_id in graph.step_outputs(step_id):
        return True
    producer = graph.producer_of(snapshot_id)
    if producer is None:  # pragma: no cover - evaluation snapshots are non-source
        return False
    return graph.step_precedes(step_id, producer)


def _value_for_key(
    key: str, before: TargetDistribution, after: TargetDistribution
) -> JsonValue:
    for item in (*before.classes, *after.classes):
        if class_sort_key(item.value) == key:
            return item.value
    return None  # pragma: no cover - keys are drawn from the class union
