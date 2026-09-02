"""Freeze eligibility for builder-generated Data Access packets."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, JsonValue

from dsx.builders.build import build_packet
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
    PacketBuildRequest,
    TargetProfile,
)
from dsx.builders.profiling import sha256_file
from dsx.packet import DsTask, DsxPacket, TaskPacket, assemble_task_packet

from .models import CaseConfig, DataAccessContract, DatasetFormat, PacketBuildMetrics

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
        except duckdb.Error as error:
            raise ValueError(f"could not inspect dataset: {error}") from error
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


class FreezeNote(DataAccessContract):
    case_id: str
    source_id: str
    license_accepted: Literal[True]
    inheritance: Literal["datasets_only"]
    packet_digest: str
    dataset_file_digest: str
    module_ids: tuple[str, ...]
    eligibility_signals: tuple[EligibilitySignal, ...]
    target_column: str
    row_count: int
    column_count: int


class FreezeResult(DataAccessContract):
    directory: str
    case: CaseConfig
    note: FreezeNote
    packet_build_metrics: PacketBuildMetrics


def _reject_oversize(inspection: SourceInspection) -> None:
    column_count = len(inspection.column_names)
    if inspection.row_count > MAX_ROWS:
        raise ValueError(f"row_count exceeds maximum: {inspection.row_count} > {MAX_ROWS}")
    if column_count > MAX_COLUMNS:
        raise ValueError(f"column_count exceeds maximum: {column_count} > {MAX_COLUMNS}")
    if inspection.target_distinct_non_null > MAX_TARGET_CLASSES:
        raise ValueError(
            "target_distinct_non_null exceeds maximum: "
            f"{inspection.target_distinct_non_null} > {MAX_TARGET_CLASSES}"
        )


def _write_indent_json(path: Path, contract: DataAccessContract) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def freeze_case(
    *,
    dataset_path: Path,
    output: Path,
    case_id: str,
    target_column: str,
    source_id: str,
    packet_id: str | None = None,
    clock: Callable[[], float] | None = None,
) -> FreezeResult:
    """Copy a dataset, build a packet bundle, and write an exclusive freeze directory."""
    output.mkdir()
    try:
        return _freeze_into(
            dataset_path=dataset_path,
            output=output,
            case_id=case_id,
            target_column=target_column,
            source_id=source_id,
            packet_id=packet_id,
            clock=clock,
        )
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def _freeze_into(
    *,
    dataset_path: Path,
    output: Path,
    case_id: str,
    target_column: str,
    source_id: str,
    packet_id: str | None,
    clock: Callable[[], float] | None,
) -> FreezeResult:
    inspection = inspect_tabular_source(dataset_path, target_column=target_column)
    _reject_oversize(inspection)
    copied = output / dataset_path.name
    shutil.copy2(dataset_path, copied)
    timer = time.perf_counter if clock is None else clock
    started = timer()
    built = build_packet(
        PacketBuildRequest(
            dataset_path=copied,
            target_column=target_column,
            packet_id=case_id if packet_id is None else packet_id,
        )
    )
    metrics = PacketBuildMetrics(
        elapsed_seconds=timer() - started,
        estimated_cost_usd=0.0,
    )
    (output / "packet.json").write_text(built.packet.canonical_json() + "\n", encoding="utf-8")
    (output / "build-record.json").write_text(
        built.build_record.canonical_json() + "\n", encoding="utf-8"
    )
    require_review_classifier_task(built.packet)
    eligibility = evaluate_eligibility(built.packet, target_column=target_column)
    case = CaseConfig(
        case_id=case_id,
        task_prompt=REVIEW_CLASSIFIER_TASK_PROMPT,
        dataset_path=str(copied),
        dataset_format=inspection.dataset_format,
        target_column=target_column,
        oracle_version="v1",
    )
    note = FreezeNote(
        case_id=case_id,
        source_id=source_id,
        license_accepted=True,
        inheritance="datasets_only",
        packet_digest=built.packet.digest(),
        dataset_file_digest=sha256_file(copied),
        module_ids=eligibility.module_ids,
        eligibility_signals=eligibility.signals,
        target_column=target_column,
        row_count=eligibility.row_count,
        column_count=eligibility.column_count,
    )
    _write_indent_json(output / "case.json", case)
    _write_indent_json(output / "packet-build-metrics.json", metrics)
    _write_indent_json(output / "freeze-note.json", note)
    return FreezeResult(
        directory=str(output),
        case=case,
        note=note,
        packet_build_metrics=metrics,
    )
