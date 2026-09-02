"""Tests for transformation-manifest contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from dsx.pipeline import (
    DatasetFormat,
    DatasetSnapshot,
    SnapshotRole,
    TransformationManifest,
    TransformationOperation,
    TransformationStep,
)


def _snapshot(**changes: object) -> DatasetSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "raw",
        "digest": "a" * 64,
        "role": "source",
        "path": "data/raw.parquet",
        "format": "parquet",
    }
    values.update(changes)
    return DatasetSnapshot.model_validate(values)


def test_manifest_round_trips_linear_json() -> None:
    payload = {
        "schema_version": "dsx-transform-manifest/v1",
        "pipeline_id": "fraud-training",
        "current_snapshot_id": "train-balanced",
        "snapshots": [
            {
                "snapshot_id": "raw",
                "digest": "a" * 64,
                "role": "source",
                "path": "data/raw.parquet",
                "format": "parquet",
            },
            {
                "snapshot_id": "train-balanced",
                "digest": "b" * 64,
                "role": "train",
                "path": "data/train-balanced.parquet",
                "format": "parquet",
            },
        ],
        "steps": [
            {
                "step_id": "prep",
                "operation": "filter",
                "inputs": ["raw"],
                "outputs": ["train-balanced"],
                "parameters": {"predicate": "label is not null"},
            }
        ],
    }
    restored = TransformationManifest.model_validate(payload)
    assert json.loads(restored.canonical_json())["pipeline_id"] == "fraud-training"
    assert restored.digest() == TransformationManifest.model_validate_json(
        restored.canonical_json()
    ).digest()
    assert restored.steps[0].operation is TransformationOperation.filter


def test_snapshot_requires_format_when_path_is_present() -> None:
    with pytest.raises(ValidationError, match="format is required"):
        _snapshot(format=None)


def test_snapshot_allows_omitted_path() -> None:
    snapshot = _snapshot(path=None, format=None)
    assert snapshot.path is None
    assert snapshot.format is None


def test_step_rejects_nonstandard_parameters() -> None:
    with pytest.raises(ValidationError, match="parameters must be standard JSON"):
        TransformationStep.model_validate(
            {
                "step_id": "bad",
                "operation": "custom",
                "inputs": ["raw"],
                "outputs": ["out"],
                "parameters": {"n": float("nan")},
            }
        )


def test_step_defaults_parameters_to_empty_object() -> None:
    step = TransformationStep(
        step_id="split",
        operation=TransformationOperation.split,
        inputs=("raw",),
        outputs=("train",),
    )
    assert step.parameters == {}
    assert DatasetFormat.parquet.value == "parquet"
    assert SnapshotRole.train.value == "train"
