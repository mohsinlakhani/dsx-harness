"""Freeze eligibility for builder-generated Data Access packets."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, JsonValue

from dsx.builders.models import (
    COLUMN_PROFILE_MODULE_ID,
    DATA_TRAPS_MODULE_ID,
    DATASET_PROFILE_MODULE_ID,
    FEATURE_RISKS_MODULE_ID,
    TARGET_PROFILE_MODULE_ID,
    ColumnsProfile,
    DatasetProfile,
    DataTrap,
    FeatureRisks,
    TargetProfile,
)
from dsx.packet import DsTask, DsxPacket, TaskPacket, assemble_task_packet

from .models import DataAccessContract, DatasetFormat

MAX_ROWS: int = 20_000
MAX_COLUMNS: int = 40
MAX_TARGET_CLASSES: int = 10
MISSING_RATE_THRESHOLD: float = 0.05
REVIEW_CLASSIFIER_TASK_PROMPT: str = (
    "Recommend a classifier for a 5% manual-review budget. "
    "Return the required structured decision. "
    "Use factual_claims only for facts you can cite exactly."
)
REVIEW_CLASSIFIER_TASK = DsTask(
    task_id="review-classifier",
    task_type="review-classifier",
    objective="Recommend a classifier for a 5% manual-review budget.",
    module_types=(
        "profile.dataset",
        "profile.columns",
        "profile.target",
        "risk.data_traps",
        "risk.features",
    ),
)

EligibilitySignal = Literal["likely_identifier", "target_class_imbalance", "material_missingness"]


class SourceInspection(DataAccessContract):
    row_count: int
    column_names: tuple[str, ...]
    target_distinct_non_null: int
    dataset_format: DatasetFormat


class Eligibility(DataAccessContract):
    signals: tuple[EligibilitySignal, ...]
    row_count: int
    column_count: int
    target_distinct_non_null: int
    module_ids: tuple[str, ...]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_tabular_source(path: Path, *, target_column: str) -> SourceInspection:
    """Read CSV/Parquet shape and target cardinality without sampling."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        dataset_format = DatasetFormat.csv
        reader_sql = "CREATE TABLE dataset AS SELECT * FROM read_csv_auto(?)"
    elif suffix == ".parquet":
        dataset_format = DatasetFormat.parquet
        reader_sql = "CREATE TABLE dataset AS SELECT * FROM read_parquet(?)"
    else:
        raise ValueError(f"unsupported dataset format: {path}")

    connection = duckdb.connect()
    try:
        connection.execute(reader_sql, [str(path)])
        count_row = connection.execute("SELECT count(*) FROM dataset").fetchone()
        if count_row is None:  # pragma: no cover - count always returns one row
            raise RuntimeError("dataset count query did not return a row")
        row_count = int(count_row[0])
        column_names = tuple(
            str(item[0]) for item in connection.execute("DESCRIBE dataset").fetchall()
        )
        if target_column not in column_names:
            raise ValueError(f"target column is not present in dataset: {target_column}")
        quoted = _quote_identifier(target_column)
        distinct_row = connection.execute(
            f"SELECT count(DISTINCT {quoted}) FROM dataset"
        ).fetchone()
        if distinct_row is None:  # pragma: no cover - count always returns one row
            raise RuntimeError("target distinct query did not return a row")
        return SourceInspection(
            row_count=row_count,
            column_names=column_names,
            target_distinct_non_null=int(distinct_row[0]),
            dataset_format=dataset_format,
        )
    finally:
        connection.close()


def _module_content(packet: DsxPacket, module_id: str) -> JsonValue:
    for module in packet.modules:
        if module.module_id == module_id:
            return module.content
    raise ValueError(f"packet does not contain module: {module_id}")


def packet_module_content[T: BaseModel](
    packet: DsxPacket, module_id: str, model: type[T]
) -> T:
    """Return one packet module payload validated as ``model``."""
    return model.model_validate(_module_content(packet, module_id))


def _data_traps(packet: DsxPacket) -> tuple[DataTrap, ...]:
    content = _module_content(packet, DATA_TRAPS_MODULE_ID)
    if not isinstance(content, list):
        raise ValueError("data-traps content must be a JSON array")
    return tuple(DataTrap.model_validate(item) for item in content)


def evaluate_eligibility(packet: DsxPacket, *, target_column: str) -> Eligibility:
    """Accept a builder packet that fits freeze caps and has at least one signal."""
    del target_column
    dataset_profile = packet_module_content(packet, DATASET_PROFILE_MODULE_ID, DatasetProfile)
    packet_module_content(packet, COLUMN_PROFILE_MODULE_ID, ColumnsProfile)
    target_profile = packet_module_content(packet, TARGET_PROFILE_MODULE_ID, TargetProfile)
    traps = _data_traps(packet)
    feature_risks = packet_module_content(packet, FEATURE_RISKS_MODULE_ID, FeatureRisks)

    row_count = dataset_profile.row_count
    column_count = len(dataset_profile.columns)
    if row_count > MAX_ROWS:
        raise ValueError(f"row_count exceeds maximum: {row_count} > {MAX_ROWS}")
    if column_count > MAX_COLUMNS:
        raise ValueError(f"column_count exceeds maximum: {column_count} > {MAX_COLUMNS}")
    target_distinct_non_null = len(target_profile.classes)
    if target_distinct_non_null > MAX_TARGET_CLASSES:
        raise ValueError(
            "target_distinct_non_null exceeds maximum: "
            f"{target_distinct_non_null} > {MAX_TARGET_CLASSES}"
        )

    signals: list[EligibilitySignal] = []
    if any(finding.kind == "likely_identifier" for finding in feature_risks.findings):
        signals.append("likely_identifier")
    if any(trap.kind == "target_class_imbalance" for trap in traps):
        signals.append("target_class_imbalance")
    if any(column.missing_rate >= MISSING_RATE_THRESHOLD for column in dataset_profile.columns):
        signals.append("material_missingness")
    if not signals:
        raise ValueError("no eligibility signal")
    return Eligibility(
        signals=tuple(signals),
        row_count=row_count,
        column_count=column_count,
        target_distinct_non_null=target_distinct_non_null,
        module_ids=tuple(module.module_id for module in packet.modules),
    )


def require_review_classifier_task(packet: DsxPacket) -> TaskPacket:
    """Assemble the shared review-classifier task view over a builder packet."""
    return assemble_task_packet(packet, REVIEW_CLASSIFIER_TASK)
