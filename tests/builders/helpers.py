"""Helpers for packet-builder tests."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import duckdb

from dsx.builders.profiling import sha256_file
from dsx.pipeline import (
    DatasetFormat,
    DatasetSnapshot,
    SnapshotRole,
    TransformationManifest,
    TransformationOperation,
    TransformationStep,
)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    if not rows:
        path.write_text("label\n", encoding="utf-8")
        return path
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> Path:
    csv_path = path.with_suffix(".source.csv")
    write_csv(csv_path, rows)
    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TABLE source AS SELECT * FROM read_csv_auto(?)", [str(csv_path)]
        )
        connection.execute("COPY source TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        connection.close()
    return path


def digest_of(path: Path) -> str:
    return sha256_file(path)


def snapshot(
    snapshot_id: str,
    digest: str,
    role: SnapshotRole,
    path: str | None = None,
    fmt: DatasetFormat | None = None,
) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id=snapshot_id,
        digest=digest,
        role=role,
        path=path,
        format=fmt,
    )


def step(
    step_id: str,
    operation: TransformationOperation,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    parameters: dict[str, Any] | None = None,
) -> TransformationStep:
    return TransformationStep(
        step_id=step_id,
        operation=operation,
        inputs=inputs,
        outputs=outputs,
        parameters=parameters or {},
    )


def manifest(
    *,
    current: str,
    snapshots: tuple[DatasetSnapshot, ...],
    steps: tuple[TransformationStep, ...] = (),
    pipeline_id: str = "fraud-training",
) -> TransformationManifest:
    return TransformationManifest(
        pipeline_id=pipeline_id,
        current_snapshot_id=current,
        snapshots=snapshots,
        steps=steps,
    )
