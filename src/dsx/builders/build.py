"""Assemble a DSX Packet from a dataset, optional manifest, and trap detectors."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from pydantic import ValidationError

from dsx.packet.models import DatasetRef, DsxPacket, PacketModule
from dsx.pipeline import DatasetFormat, TransformationGraph, TransformationManifest

from .models import (
    CURRENT_SNAPSHOT_ID,
    DATA_TRAPS_MODULE_ID,
    DATA_TRAPS_SCHEMA,
    DATA_TRAPS_TYPE,
    DATASET_PROFILE_MODULE_ID,
    DATASET_PROFILE_SCHEMA,
    DATASET_PROFILE_TYPE,
    TARGET_PROFILE_MODULE_ID,
    TARGET_PROFILE_SCHEMA,
    TARGET_PROFILE_TYPE,
    TRANSFORMATION_HISTORY_MODULE_ID,
    TRANSFORMATION_HISTORY_SCHEMA,
    TRANSFORMATION_HISTORY_TYPE,
    AugmentationDistribution,
    DatasetProfile,
    DataTrap,
    HistoryStep,
    ModuleDescriptor,
    PacketBuildRecord,
    PacketBuildRequest,
    PacketBuildResult,
    TargetDistribution,
    TargetProfile,
    TransformationHistory,
)
from .profiling import (
    SupportedFormat,
    count_table_rows,
    detect_dataset_format,
    profile_table,
    profile_target,
    sha256_file,
)
from .traps import (
    detect_augmentation_before_split,
    detect_augmentation_changed_target_distribution,
    detect_augmentation_on_evaluation,
    detect_target_class_imbalance,
)


class HistoricalSnapshot(NamedTuple):
    path: Path
    format: SupportedFormat


def snapshot_evidence_ref(digest: str) -> str:
    return f"sha256:{digest}"


def manifest_evidence_ref(digest: str) -> str:
    return f"manifest:{digest}"


def resolve_snapshot_path(declared_path: str, snapshot_root: Path | None) -> Path:
    path = Path(declared_path)
    if path.is_absolute():
        return path
    if snapshot_root is None:
        raise ValueError(f"relative snapshot path requires snapshot_root: {declared_path}")
    return snapshot_root / path


def classify_historical_snapshots(
    manifest: TransformationManifest,
    *,
    snapshot_root: Path | None,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, HistoricalSnapshot]]:
    """Return accessible IDs, unavailable IDs, and readable historical paths."""
    accessible: list[str] = []
    unavailable: list[str] = []
    readable: dict[str, HistoricalSnapshot] = {}
    current_id = manifest.current_snapshot_id
    for snapshot in manifest.snapshots:
        if snapshot.snapshot_id == current_id:
            accessible.append(snapshot.snapshot_id)
            continue
        if snapshot.path is None:
            unavailable.append(snapshot.snapshot_id)
            continue
        resolved = resolve_snapshot_path(snapshot.path, snapshot_root)
        if not resolved.is_file():
            unavailable.append(snapshot.snapshot_id)
            continue
        actual = sha256_file(resolved)
        if actual != snapshot.digest:
            raise ValueError(
                f"historical snapshot {snapshot.snapshot_id} digest mismatch: "
                f"declared {snapshot.digest}, actual {actual}"
            )
        assert snapshot.format is not None
        declared_format: SupportedFormat = (
            "csv" if snapshot.format is DatasetFormat.csv else "parquet"
        )
        if count_table_rows(resolved, declared_format) == 0:
            unavailable.append(snapshot.snapshot_id)
            continue
        accessible.append(snapshot.snapshot_id)
        readable[snapshot.snapshot_id] = HistoricalSnapshot(resolved, declared_format)
    return tuple(accessible), tuple(unavailable), readable


def load_previous_bundle(path: Path) -> tuple[DsxPacket, PacketBuildRecord]:
    """Parse and internally validate a previous packet bundle."""
    packet_path = path / "packet.json"
    record_path = path / "build-record.json"
    if not path.is_dir():
        raise ValueError(f"previous bundle does not exist: {path}")
    if not packet_path.is_file() or not record_path.is_file():
        raise ValueError(f"previous bundle is incomplete: {path}")
    try:
        packet = DsxPacket.model_validate_json(packet_path.read_text(encoding="utf-8"))
        record = PacketBuildRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
    except (ValidationError, OSError, UnicodeError) as error:
        raise ValueError(f"previous bundle could not be parsed: {path}") from error
    digest = packet.digest()
    if digest != record.packet_digest:
        raise ValueError("previous bundle packet digest does not match its build record")
    return packet, record


def build_packet(request: PacketBuildRequest) -> PacketBuildResult:
    """Build a DSX Packet and build record without writing an output bundle."""
    dataset_path = request.dataset_path
    if not dataset_path.is_file():
        raise ValueError(f"dataset does not exist or is unreadable: {dataset_path}")
    dataset_format = detect_dataset_format(dataset_path)
    dataset_digest = sha256_file(dataset_path)
    graph: TransformationGraph | None = None
    current_snapshot_id = CURRENT_SNAPSHOT_ID
    accessible: tuple[str, ...] = (CURRENT_SNAPSHOT_ID,)
    unavailable: tuple[str, ...] = ()
    historical_paths: dict[str, HistoricalSnapshot] = {}
    manifest_digest: str | None = None

    if request.manifest is not None:
        graph = TransformationGraph.from_manifest(request.manifest)
        current_snapshot_id = request.manifest.current_snapshot_id
        current_declared = graph.snapshot(current_snapshot_id)
        if current_declared.digest != dataset_digest:
            raise ValueError(
                "current dataset digest does not match the manifest current snapshot"
            )
        accessible, unavailable, historical_paths = classify_historical_snapshots(
            request.manifest, snapshot_root=request.snapshot_root
        )
        manifest_digest = request.manifest.digest()

    dataset_profile = profile_table(
        dataset_path, snapshot_id=current_snapshot_id, format=dataset_format
    )
    current_target = profile_target(
        dataset_path,
        snapshot_id=current_snapshot_id,
        target_column=request.target_column,
        format=dataset_format,
        require_target=True,
    )
    if current_target is None:  # pragma: no cover - require_target always raises
        raise ValueError(f"target column is not present in dataset: {request.target_column}")

    augmentation_distributions: tuple[AugmentationDistribution, ...] = ()
    historical_targets: dict[str, TargetDistribution] = {current_snapshot_id: current_target}
    if graph is not None:
        augmentation_distributions, historical_targets = _augmentation_distributions(
            graph=graph,
            current_snapshot_id=current_snapshot_id,
            historical_paths=historical_paths,
            target_column=request.target_column,
            current_target=current_target,
        )

    target_profile = TargetProfile(
        target_column=request.target_column,
        current_snapshot_id=current_snapshot_id,
        null_count=current_target.null_count,
        non_null_count=current_target.non_null_count,
        classes=current_target.classes,
        majority_class_rate=current_target.majority_class_rate,
        augmentation_distributions=augmentation_distributions,
    )

    dataset_evidence = (snapshot_evidence_ref(dataset_digest),)
    history_evidence = () if manifest_digest is None else (manifest_evidence_ref(manifest_digest),)
    traps = _collect_traps(
        current_target=current_target,
        graph=graph,
        current_snapshot_id=current_snapshot_id,
        historical_targets=historical_targets,
        dataset_evidence=dataset_evidence,
        history_evidence=history_evidence,
    )

    modules = _assemble_modules(
        dataset_profile=dataset_profile,
        target_profile=target_profile,
        traps=traps,
        graph=graph,
        manifest=request.manifest,
        manifest_digest=manifest_digest,
        accessible=accessible,
        unavailable=unavailable,
        dataset_evidence=dataset_evidence,
        history_evidence=history_evidence,
        historical_targets=historical_targets,
    )
    packet = DsxPacket(
        packet_id=request.packet_id,
        dataset=DatasetRef(digest=dataset_digest, name=dataset_path.name),
        modules=modules,
    )
    previous_digest = request.previous_packet_digest
    if request.previous_build_record is None:
        revision = 1
    else:
        revision = request.previous_build_record.revision + 1
    record = PacketBuildRecord(
        build_id=secrets.token_urlsafe(18),
        built_at=datetime.now(UTC),
        revision=revision,
        packet_id=request.packet_id,
        packet_digest=packet.digest(),
        previous_packet_digest=previous_digest,
        dataset_path=str(dataset_path),
        dataset_digest=dataset_digest,
        target_column=request.target_column,
        manifest_digest=manifest_digest,
        modules=tuple(
            ModuleDescriptor(
                module_id=module.module_id,
                module_type=module.module_type,
                schema_version=module.schema_version,
            )
            for module in modules
        ),
    )
    return PacketBuildResult(packet=packet, build_record=record, manifest=request.manifest)


def _augmentation_distributions(
    *,
    graph: TransformationGraph,
    current_snapshot_id: str,
    historical_paths: dict[str, HistoricalSnapshot],
    target_column: str,
    current_target: TargetDistribution,
) -> tuple[tuple[AugmentationDistribution, ...], dict[str, TargetDistribution]]:
    _, lineage_steps = graph.lineage_to(current_snapshot_id)
    targets: dict[str, TargetDistribution] = {current_snapshot_id: current_target}
    distributions: list[AugmentationDistribution] = []
    for step_id in graph.augment_steps():
        if step_id not in lineage_steps:
            continue
        inputs = graph.step_inputs(step_id)
        outputs = graph.step_outputs(step_id)
        if len(inputs) != 1 or len(outputs) != 1:
            continue
        before = _target_for_snapshot(
            snapshot_id=inputs[0],
            historical_paths=historical_paths,
            target_column=target_column,
            cache=targets,
        )
        after = _target_for_snapshot(
            snapshot_id=outputs[0],
            historical_paths=historical_paths,
            target_column=target_column,
            cache=targets,
        )
        if before is None or after is None:
            continue
        distributions.append(
            AugmentationDistribution(step_id=step_id, before=before, after=after)
        )
    return tuple(distributions), targets


def _target_for_snapshot(
    *,
    snapshot_id: str,
    historical_paths: dict[str, HistoricalSnapshot],
    target_column: str,
    cache: dict[str, TargetDistribution],
) -> TargetDistribution | None:
    cached = cache.get(snapshot_id)
    if cached is not None:
        return cached
    historical = historical_paths.get(snapshot_id)
    if historical is None:
        return None
    profiled = profile_target(
        historical.path,
        snapshot_id=snapshot_id,
        target_column=target_column,
        format=historical.format,
        require_target=False,
    )
    if profiled is None or profiled.non_null_count == 0:
        return None
    cache[snapshot_id] = profiled
    return profiled


def _collect_traps(
    *,
    current_target: TargetDistribution,
    graph: TransformationGraph | None,
    current_snapshot_id: str,
    historical_targets: dict[str, TargetDistribution],
    dataset_evidence: tuple[str, ...],
    history_evidence: tuple[str, ...],
) -> tuple[DataTrap, ...]:
    traps: list[DataTrap] = []
    imbalance = detect_target_class_imbalance(
        current_target, evidence_refs=dataset_evidence
    )
    if imbalance is not None:
        traps.append(imbalance)
    if graph is None:
        return tuple(traps)
    traps.extend(
        detect_augmentation_before_split(
            graph,
            current_snapshot_id=current_snapshot_id,
            evidence_refs=history_evidence,
        )
    )
    traps.extend(
        detect_augmentation_on_evaluation(graph, evidence_refs=history_evidence)
    )
    traps.extend(
        detect_augmentation_changed_target_distribution(
            graph=graph,
            current_snapshot_id=current_snapshot_id,
            distributions=historical_targets,
            evidence_refs=history_evidence,
        )
    )
    return tuple(traps)


def _assemble_modules(
    *,
    dataset_profile: DatasetProfile,
    target_profile: TargetProfile,
    traps: tuple[DataTrap, ...],
    graph: TransformationGraph | None,
    manifest: TransformationManifest | None,
    manifest_digest: str | None,
    accessible: tuple[str, ...],
    unavailable: tuple[str, ...],
    dataset_evidence: tuple[str, ...],
    history_evidence: tuple[str, ...],
    historical_targets: dict[str, TargetDistribution],
) -> tuple[PacketModule, ...]:
    modules: list[PacketModule] = [
        PacketModule(
            module_id=DATASET_PROFILE_MODULE_ID,
            module_type=DATASET_PROFILE_TYPE,
            schema_version=DATASET_PROFILE_SCHEMA,
            content=dataset_profile.model_dump(mode="json"),
            evidence_refs=dataset_evidence,
        ),
        PacketModule(
            module_id=TARGET_PROFILE_MODULE_ID,
            module_type=TARGET_PROFILE_TYPE,
            schema_version=TARGET_PROFILE_SCHEMA,
            content=target_profile.model_dump(mode="json"),
            evidence_refs=_target_evidence(
                dataset_evidence=dataset_evidence,
                historical_targets=historical_targets,
                manifest=manifest,
            ),
        ),
    ]
    if manifest is not None and graph is not None and manifest_digest is not None:
        _, lineage_steps = graph.lineage_to(manifest.current_snapshot_id)
        history = TransformationHistory(
            pipeline_id=manifest.pipeline_id,
            manifest_digest=manifest_digest,
            current_snapshot_id=manifest.current_snapshot_id,
            steps=tuple(
                HistoryStep(
                    step_id=graph.step(step_id).step_id,
                    operation=graph.step(step_id).operation.value,
                    inputs=graph.step(step_id).inputs,
                    outputs=graph.step(step_id).outputs,
                    parameters=dict(graph.step(step_id).parameters),
                )
                for step_id in lineage_steps
            ),
            accessible_snapshot_ids=accessible,
            unavailable_snapshot_ids=unavailable,
        )
        modules.append(
            PacketModule(
                module_id=TRANSFORMATION_HISTORY_MODULE_ID,
                module_type=TRANSFORMATION_HISTORY_TYPE,
                schema_version=TRANSFORMATION_HISTORY_SCHEMA,
                content=history.model_dump(mode="json"),
                evidence_refs=history_evidence,
            )
        )
    modules.append(
        PacketModule(
            module_id=DATA_TRAPS_MODULE_ID,
            module_type=DATA_TRAPS_TYPE,
            schema_version=DATA_TRAPS_SCHEMA,
            content=[trap.model_dump(mode="json") for trap in traps],
            evidence_refs=_unique(
                *(trap.evidence_refs for trap in traps),
                extra=dataset_evidence if not traps else (),
            ),
        )
    )
    return tuple(modules)


def _target_evidence(
    *,
    dataset_evidence: tuple[str, ...],
    historical_targets: dict[str, TargetDistribution],
    manifest: TransformationManifest | None,
) -> tuple[str, ...]:
    refs = list(dataset_evidence)
    if manifest is None:
        return tuple(refs)
    by_id = {snapshot.snapshot_id: snapshot for snapshot in manifest.snapshots}
    for snapshot_id in historical_targets:
        snapshot = by_id.get(snapshot_id)
        if snapshot is None:  # pragma: no cover - targets are keyed by manifest snapshots
            continue
        ref = snapshot_evidence_ref(snapshot.digest)
        if ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _unique(*groups: tuple[str, ...], extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    seen: list[str] = []
    for group in (*groups, extra):
        for item in group:
            if item not in seen:
                seen.append(item)
    return tuple(seen)
