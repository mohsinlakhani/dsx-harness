"""Mask, freeze, and reveal workflow for private Data Access run evidence."""

from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .canonical import canonical_digest, parse_canonical_json
from .cli import RUN_MANIFEST_FILENAME, DataAccessRunManifest, _validate_prepared_input
from .evaluation import (
    DataAccessDecision,
    cited_tool_evidence_ids,
    evaluate_decision,
    replay_sql_evidence,
)
from .execution import (
    ArmExecutionSpec,
    ArmRun,
    AttemptStatus,
    ExecutionOutcome,
    ModelRequestStart,
    RepetitionAttempt,
    RepetitionOutcome,
    RepetitionRun,
    ToolCallLedger,
    build_arm_specs,
    validate_arm_transcript,
    validate_fairness,
)
from .models import Arm, DataAccessContract, DataAccessManifest
from .report import DataAccessReport, build_experiment_report, summarize_arm

MANIFEST_FILENAME = "manifest.json"
FROZEN_JUDGMENTS_FILENAME = "frozen_judgments.json"
REVEAL_DIRECTORY = "reveal"
REVEAL_MAP_FILENAME = "reveal_map.json"
REVEALED_REPORT_FILENAME = "revealed_report.json"
BLIND_VERSION: Literal["data-access-blind-v2"] = "data-access-blind-v2"
_OPAQUE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class BlindOutputDigest(DataAccessContract):
    opaque_id: str = Field(min_length=1)
    digest: str = Field(min_length=64, max_length=64)


class BlindManifest(DataAccessContract):
    version: Literal["data-access-blind-v2"]
    blind_seed: int
    ordered_opaque_ids: tuple[str, ...]
    eligible_count: int = Field(ge=0)
    exclusions: tuple[str, ...] = ()
    output_digests: tuple[BlindOutputDigest, ...]

    @model_validator(mode="after")
    def coherent_ids(self) -> BlindManifest:
        if len(self.ordered_opaque_ids) != self.eligible_count:
            raise ValueError("eligible_count must match opaque ID count")
        if len(set(self.ordered_opaque_ids)) != len(self.ordered_opaque_ids):
            raise ValueError("opaque IDs must be unique")
        if tuple(item.opaque_id for item in self.output_digests) != self.ordered_opaque_ids:
            raise ValueError("output digests must follow opaque ID order")
        return self


class PublicClaim(DataAccessContract):
    """A claim statement without predicate, value, or evidence-source locator."""

    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class PublicDecision(DataAccessContract):
    primary_metric: str
    supporting_metrics: tuple[str, ...]
    review_budget_fraction: float
    split_strategy: str
    excluded_columns: tuple[str, ...]
    reasoning: str
    limitations: tuple[str, ...]
    recommendation: str
    factual_claims: tuple[PublicClaim, ...]
    narrative_claim_ids: tuple[str, ...]

    @classmethod
    def from_private(cls, decision: DataAccessDecision) -> PublicDecision:
        return cls(
            primary_metric=decision.primary_metric.value,
            supporting_metrics=tuple(metric.value for metric in decision.supporting_metrics),
            review_budget_fraction=decision.review_budget_fraction,
            split_strategy=decision.split_strategy,
            excluded_columns=decision.excluded_columns,
            reasoning=decision.reasoning,
            limitations=decision.limitations,
            recommendation=decision.recommendation,
            factual_claims=tuple(
                PublicClaim(claim_id=claim.claim_id, statement=claim.statement)
                for claim in decision.factual_claims
            ),
            narrative_claim_ids=decision.narrative_claim_ids,
        )


class PublicBlindOutput(DataAccessContract):
    opaque_id: str = Field(min_length=1)
    decision: PublicDecision


class BlindJudgment(DataAccessContract):
    opaque_id: str = Field(min_length=1)
    decision_quality: int = Field(ge=1, le=5, strict=True)
    evidence_use: int = Field(ge=1, le=5, strict=True)
    limitations_quality: int = Field(ge=1, le=5, strict=True)
    arm_guess: Arm
    guess_confidence: int = Field(ge=1, le=5, strict=True)


class FrozenJudgments(DataAccessContract):
    manifest_digest: str = Field(min_length=64, max_length=64)
    judgments_digest: str = Field(min_length=64, max_length=64)
    judgments: tuple[BlindJudgment, ...]


class RevealEntry(DataAccessContract):
    opaque_id: str = Field(min_length=1)
    repetition_number: int = Field(gt=0)
    repetition_id: str = Field(min_length=1)
    arm: Arm


class RevealMap(DataAccessContract):
    version: Literal["data-access-blind-v2"]
    manifest_digest: str = Field(min_length=64, max_length=64)
    entries: tuple[RevealEntry, ...]


class RevealedJudgment(DataAccessContract):
    opaque_id: str = Field(min_length=1)
    repetition_number: int = Field(gt=0)
    repetition_id: str = Field(min_length=1)
    arm: Arm
    judgment: BlindJudgment


class QualitativeArmMetrics(DataAccessContract):
    arm: Arm
    count: int = Field(ge=0)
    decision_quality: float = Field(ge=0.0, le=5.0)
    evidence_use: float = Field(ge=0.0, le=5.0)
    limitations_quality: float = Field(ge=0.0, le=5.0)
    arm_guess_accuracy: float = Field(ge=0.0, le=1.0)
    guess_confidence: float = Field(ge=0.0, le=5.0)


class RevealedReport(DataAccessContract):
    version: Literal["data-access-blind-v2"]
    raw_judgments: tuple[RevealedJudgment, ...]
    qualitative_metrics: tuple[QualitativeArmMetrics, ...]
    objective_report: DataAccessReport


def opaque_id_for(
    blind_seed: int, repetition_number: int, repetition_id: str, arm: Arm
) -> str:
    material = (
        f"{BLIND_VERSION}\0{blind_seed}\0{repetition_number}\0{repetition_id}\0{arm.value}"
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _write(path: Path, contract: DataAccessContract) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _read_run_manifest(run_root: Path) -> DataAccessRunManifest:
    return DataAccessRunManifest.model_validate_json(
        (run_root / RUN_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def _read_repetition_runs(run_root: Path) -> tuple[RepetitionRun, ...]:
    records = tuple(
        RepetitionRun.model_validate_json(path.read_text(encoding="utf-8"))
        for path in run_root.glob("repetition-*/repetition_run.json")
    )
    if not records:
        raise ValueError("run contains no repetition records")
    return tuple(sorted(records, key=lambda repetition: repetition.repetition_number))


def _read_contract[ContractT: DataAccessContract](
    path: Path, contract_type: type[ContractT]
) -> ContractT:
    return contract_type.model_validate_json(path.read_text(encoding="utf-8"))


def _expected_order_seeds(order_seed: int, repetitions: int) -> tuple[int, ...]:
    generator = random.Random(order_seed)
    return tuple(generator.randrange(2**63) for _ in range(repetitions))


def _validate_arm_artifacts(
    attempt_directory: Path,
    arm_run: ArmRun,
    spec: ArmExecutionSpec,
    *,
    model_calls_per_arm: int | None = None,
) -> None:
    arm_directory = attempt_directory / "arms" / arm_run.arm.value
    if not arm_directory.is_dir():
        raise ValueError("run ledger is missing an arm directory")
    if _read_contract(arm_directory / "arm_spec.json", ArmExecutionSpec) != spec:
        raise ValueError("run ledger arm specification does not match manifest")
    if _read_contract(arm_directory / "arm_run.json", ArmRun) != arm_run:
        raise ValueError("run ledger arm summary does not match repetition summary")
    validate_arm_transcript(spec, arm_run, model_calls_per_arm=model_calls_per_arm)
    model_directory = arm_directory / "model_calls"
    request_directory = arm_directory / "model_requests"
    tool_directory = arm_directory / "tool_calls"
    expected_arm_artifacts = {
        arm_directory / "arm_spec.json",
        arm_directory / "arm_run.json",
        model_directory,
        request_directory,
    }
    if arm_run.tool_calls:
        expected_arm_artifacts.add(tool_directory)
    if set(arm_directory.iterdir()) != expected_arm_artifacts:
        raise ValueError("run ledger arm artifacts do not match arm summary")
    expected_models = {
        model_directory / f"{call.model_call_number:03d}-{call.provider_attempt_number}.json"
        for call in arm_run.model_calls
    }
    actual_models = set(model_directory.iterdir()) if model_directory.exists() else set()
    if actual_models != expected_models:
        raise ValueError("run ledger model-call artifacts do not match arm summary")
    expected_requests = {
        request_directory / f"{call.model_call_number:03d}-{call.provider_attempt_number}.json"
        for call in arm_run.model_calls
    }
    actual_requests = set(request_directory.iterdir()) if request_directory.exists() else set()
    if actual_requests != expected_requests:
        raise ValueError("run ledger pre-call request artifacts do not match arm summary")
    expected_tools = {
        tool_directory / f"{call.tool_call_number:03d}.json" for call in arm_run.tool_calls
    }
    actual_tools = set(tool_directory.iterdir()) if tool_directory.exists() else set()
    if actual_tools != expected_tools:
        raise ValueError("run ledger tool-call artifacts do not match arm summary")
    for model_call in arm_run.model_calls:
        filename = f"{model_call.model_call_number:03d}-{model_call.provider_attempt_number}.json"
        persisted_request = _read_contract(
            request_directory / filename, ModelRequestStart
        )
        if (
            persisted_request.model_call_number != model_call.model_call_number
            or persisted_request.provider_attempt_number != model_call.provider_attempt_number
            or persisted_request.request != model_call.request
        ):
            raise ValueError("run ledger pre-call request does not match model call")
        persisted_model = _read_contract(
            model_directory / filename,
            type(model_call),
        )
        if (
            persisted_model != model_call
            or persisted_model.request.model_identifier != spec.model_identifier
        ):
            raise ValueError("run ledger model call does not match its arm specification")
    for tool_call in arm_run.tool_calls:
        persisted_tool = _read_contract(
            tool_directory / f"{tool_call.tool_call_number:03d}.json", ToolCallLedger
        )
        if (
            persisted_tool != tool_call
            or persisted_tool.model_call_number > len(arm_run.model_calls)
        ):
            raise ValueError("run ledger tool call does not match its arm summary")


def validate_run_root(
    run_manifest: DataAccessRunManifest, run_root: Path
) -> tuple[RepetitionRun, ...]:
    """Reconcile every persisted private execution artifact before blind use.

    Execution persists an immutable request-start record before every provider
    call. This verifier reconciles it with the later model-call ledger and rejects
    any missing, extra, or divergent ledger artifact.
    """
    manifest = run_manifest.input_manifest
    input_directory = Path(manifest.dataset.database_path).parent
    _validate_prepared_input(input_directory, manifest)
    if _read_run_manifest(run_root) != run_manifest:
        raise ValueError("run ledger manifest does not match the supplied run commitment")
    expected_specs = {spec.arm: spec for spec in build_arm_specs(manifest)}
    common_digest = validate_fairness(tuple(expected_specs.values()))
    seeds = _expected_order_seeds(run_manifest.order_seed, manifest.limits.repetitions)
    expected_repetition_ids = tuple(
        f"repetition-{number:03d}" for number in range(1, manifest.limits.repetitions + 1)
    )
    expected_directories = {
        run_root / f"repetition-{number}-{repetition_id}"
        for number, repetition_id in enumerate(expected_repetition_ids, start=1)
    }
    actual_directories = {path for path in run_root.glob("repetition-*") if path.is_dir()}
    if actual_directories != expected_directories:
        raise ValueError("run ledger repetition directories do not match run manifest")
    unexpected_root = {
        path.name
        for path in run_root.iterdir()
        if path.name != RUN_MANIFEST_FILENAME and path not in expected_directories
    }
    if unexpected_root:
        raise ValueError("run ledger contains unexpected root artifacts")
    repetitions = _read_repetition_runs(run_root)
    if len(repetitions) != manifest.limits.repetitions:
        raise ValueError("run ledger repetition count does not match run manifest")
    attempt_counter = 0
    for number, (repetition_id, seed, repetition) in enumerate(
        zip(expected_repetition_ids, seeds, repetitions, strict=True), start=1
    ):
        repetition_directory = run_root / f"repetition-{number}-{repetition_id}"
        if set(repetition_directory.iterdir()) != {
            repetition_directory / "attempts",
            repetition_directory / "repetition_run.json",
        }:
            raise ValueError("run ledger repetition artifacts do not match run manifest")
        if (
            repetition.repetition_number != number
            or repetition.repetition_id != repetition_id
        ):
            raise ValueError("run ledger repetition identity does not match run manifest")
        if (
            _read_contract(repetition_directory / "repetition_run.json", RepetitionRun)
            != repetition
        ):
            raise ValueError(
                "run ledger repetition summary does not match persisted repetition record"
            )
        expected_attempt_dirs: set[Path] = set()
        order_generator = random.Random(seed)
        for attempt_number, attempt in enumerate(repetition.attempts, start=1):
            attempt_counter += 1
            expected_id = f"attempt-{attempt_counter:04d}"
            attempt_directory = repetition_directory / "attempts" / expected_id
            expected_attempt_dirs.add(attempt_directory)
            if set(attempt_directory.iterdir()) != {
                attempt_directory / "arms",
                attempt_directory / "attempt_start.json",
                attempt_directory / "repetition_attempt.json",
            }:
                raise ValueError("run ledger attempt artifacts do not match repetition summary")
            if (
                attempt.attempt_number != attempt_number
                or attempt.attempt_id != expected_id
                or attempt.repetition_number != number
                or attempt.repetition_id != repetition_id
                or attempt.common_projection_digest != common_digest
                or attempt.arm_order
                != tuple(order_generator.sample(list(Arm), k=3))
            ):
                raise ValueError("run ledger attempt identity, order, or digest is invalid")
            start = _read_contract(attempt_directory / "attempt_start.json", RepetitionAttempt)
            summary = _read_contract(
                attempt_directory / "repetition_attempt.json", RepetitionAttempt
            )
            expected_start = RepetitionAttempt(
                repetition_number=number,
                repetition_id=repetition_id,
                attempt_number=attempt_number,
                attempt_id=expected_id,
                arm_order=attempt.arm_order,
                common_projection_digest=common_digest,
                arms=(),
                terminal_status=AttemptStatus.infra_failure,
            )
            if summary.terminal_status is AttemptStatus.infra_failure and not summary.arms:
                raise ValueError(
                    "persisted repetition attempt requires exhausted infrastructure evidence"
                )
            if summary != attempt or start != expected_start:
                raise ValueError("run ledger attempt artifacts do not match repetition summary")
            if tuple(run.arm for run in attempt.arms) != attempt.arm_order[: len(attempt.arms)]:
                raise ValueError("run ledger arms are not persisted in execution order")
            expected_arms = {attempt_directory / "arms" / run.arm.value for run in attempt.arms}
            actual_arms = set((attempt_directory / "arms").iterdir())
            if actual_arms != expected_arms:
                raise ValueError("run ledger arm directories do not match attempt summary")
            for arm_run in attempt.arms:
                if (
                    arm_run.repetition_number != number
                    or arm_run.repetition_id != repetition_id
                    or arm_run.attempt_number != attempt_number
                    or arm_run.common_projection_digest != common_digest
                ):
                    raise ValueError("run ledger arm identity or digest is invalid")
                _validate_arm_artifacts(
                    attempt_directory,
                    arm_run,
                    expected_specs[arm_run.arm],
                    model_calls_per_arm=manifest.limits.model_calls_per_arm,
                )
        actual_attempt_dirs = set((repetition_directory / "attempts").iterdir())
        if actual_attempt_dirs != expected_attempt_dirs:
            raise ValueError("run ledger attempt directories do not match repetition summary")
    return repetitions


def _final_completed(repetition: RepetitionRun) -> tuple[ArmRun, ArmRun, ArmRun] | None:
    if repetition.outcome is not RepetitionOutcome.complete:
        return None
    final = repetition.attempts[-1]
    if getattr(final, "terminal_status", None) is not AttemptStatus.complete:
        return None
    by_arm = {run.arm: run for run in final.arms}
    packet = by_arm.get(Arm.dsx_packet)
    full_data = by_arm.get(Arm.full_data)
    packet_and_full_data = by_arm.get(Arm.packet_and_full_data)
    if packet is None or full_data is None or packet_and_full_data is None:
        return None
    if any(
        run.terminal_outcome is not ExecutionOutcome.completed
        for run in (packet, full_data, packet_and_full_data)
    ):
        return None
    return packet, full_data, packet_and_full_data


def _decision(arm_run: ArmRun) -> DataAccessDecision:
    if arm_run.decision_json is None:
        raise ValueError("completed arm does not contain a decision")
    value = parse_canonical_json(arm_run.decision_json)
    if not isinstance(value, dict):
        raise ValueError("decision is not a JSON object")
    return DataAccessDecision.model_validate(value)


def _records(
    run_root: Path, blind_seed: int
) -> tuple[list[tuple[str, RepetitionRun, ArmRun, PublicBlindOutput]], tuple[str, ...]]:
    records: list[tuple[str, RepetitionRun, ArmRun, PublicBlindOutput]] = []
    exclusions: list[str] = []
    for repetition in _read_repetition_runs(run_root):
        completed = _final_completed(repetition)
        if completed is None:
            exclusions.append(repetition.outcome.value)
            continue
        for arm_run in completed:
            decision = _decision(arm_run)
            opaque_id = opaque_id_for(
                blind_seed,
                repetition.repetition_number,
                repetition.repetition_id,
                arm_run.arm,
            )
            records.append(
                (
                    opaque_id,
                    repetition,
                    arm_run,
                    PublicBlindOutput(
                        opaque_id=opaque_id, decision=PublicDecision.from_private(decision)
                    ),
                )
            )
    if len({record[0] for record in records}) != len(records):
        raise ValueError("opaque ID collision")
    return records, tuple(sorted(exclusions))


def _output_digest(output: PublicBlindOutput) -> str:
    return canonical_digest(output)


def _manifest_from_source(run_root: Path, blind_seed: int) -> tuple[
    list[tuple[str, RepetitionRun, ArmRun, PublicBlindOutput]], BlindManifest
]:
    records, exclusions = _records(run_root, blind_seed)
    public = sorted(((record[0], record[3]) for record in records), key=lambda item: item[0])
    random.Random(blind_seed).shuffle(public)
    return records, BlindManifest(
        version=BLIND_VERSION,
        blind_seed=blind_seed,
        ordered_opaque_ids=tuple(item[0] for item in public),
        eligible_count=len(public),
        exclusions=exclusions,
        output_digests=tuple(
            BlindOutputDigest(opaque_id=opaque_id, digest=_output_digest(output))
            for opaque_id, output in public
        ),
    )


def _stage(destination: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))


def _publish(stage: Path, destination: Path) -> None:
    os.symlink(stage.name, destination, target_is_directory=True)


def _manifest_digest(manifest: BlindManifest) -> str:
    return canonical_digest(manifest)


def _read_manifest(bundle: Path) -> BlindManifest:
    return BlindManifest.model_validate_json(
        (bundle / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )


def _validated_outputs(bundle: Path, manifest: BlindManifest) -> dict[str, PublicBlindOutput]:
    outputs: dict[str, PublicBlindOutput] = {}
    for committed in manifest.output_digests:
        output = PublicBlindOutput.model_validate_json(
            (bundle / f"{committed.opaque_id}.json").read_text(encoding="utf-8")
        )
        if output.opaque_id != committed.opaque_id or _output_digest(output) != committed.digest:
            raise ValueError("public output digest does not match manifest")
        outputs[output.opaque_id] = output
    return outputs


def _validated_source(
    run_root: Path, manifest: BlindManifest
) -> list[tuple[str, RepetitionRun, ArmRun, PublicBlindOutput]]:
    records, expected = _manifest_from_source(run_root, manifest.blind_seed)
    if manifest != expected:
        raise ValueError("blind manifest does not match private run evidence")
    return records


def export_blind(run_root: Path, destination: Path, *, blind_seed: int) -> BlindManifest:
    """Publish only opaque decisions and factual claim statements for blind review."""
    validate_run_root(_read_run_manifest(run_root), run_root)
    records, manifest = _manifest_from_source(run_root, blind_seed)
    if destination.exists():
        raise FileExistsError(destination)
    stage = _stage(destination)
    published = False
    try:
        _write(stage / MANIFEST_FILENAME, manifest)
        for opaque_id, _pair, _arm, output in records:
            _write(stage / f"{opaque_id}.json", output)
        _validated_outputs(stage, manifest)
        _publish(stage, destination)
        published = True
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)
    return manifest


def _ordered_judgments(
    manifest: BlindManifest, raw: Sequence[BlindJudgment | dict[str, Any]]
) -> tuple[BlindJudgment, ...]:
    judgments = tuple(
        item if isinstance(item, BlindJudgment) else BlindJudgment.model_validate(item)
        for item in raw
    )
    identifiers = tuple(item.opaque_id for item in judgments)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate opaque judgment IDs")
    expected = set(manifest.ordered_opaque_ids)
    actual = set(identifiers)
    if expected - actual:
        raise ValueError("missing opaque judgment IDs")
    if actual - expected:
        raise ValueError("extra opaque judgment IDs")
    by_id = {item.opaque_id: item for item in judgments}
    return tuple(by_id[opaque_id] for opaque_id in manifest.ordered_opaque_ids)


def _judgments_digest(judgments: Sequence[BlindJudgment]) -> str:
    return canonical_digest([item.model_dump(mode="json") for item in judgments])


def freeze_judgments(
    bundle: Path, raw_judgments: Sequence[BlindJudgment | dict[str, Any]], *, run_root: Path
) -> FrozenJudgments:
    """Exclusively freeze a complete, source-bound set of blind judgments."""
    destination = bundle / FROZEN_JUDGMENTS_FILENAME
    if destination.exists():
        raise FileExistsError(destination)
    manifest = _read_manifest(bundle)
    validate_run_root(_read_run_manifest(run_root), run_root)
    _validated_source(run_root, manifest)
    _validated_outputs(bundle, manifest)
    judgments = _ordered_judgments(manifest, raw_judgments)
    frozen = FrozenJudgments(
        manifest_digest=_manifest_digest(manifest),
        judgments_digest=_judgments_digest(judgments),
        judgments=judgments,
    )
    _write(destination, frozen)
    return frozen


def _read_frozen(bundle: Path, manifest: BlindManifest) -> FrozenJudgments:
    frozen = FrozenJudgments.model_validate_json(
        (bundle / FROZEN_JUDGMENTS_FILENAME).read_text(encoding="utf-8")
    )
    ordered = _ordered_judgments(manifest, frozen.judgments)
    if (
        frozen.manifest_digest != _manifest_digest(manifest)
        or frozen.judgments != ordered
        or frozen.judgments_digest != _judgments_digest(ordered)
    ):
        raise ValueError("frozen judgments do not match public blind commitments")
    return frozen


def _mean(items: Sequence[BlindJudgment], field: str) -> float:
    return sum(float(getattr(item, field)) for item in items) / len(items) if items else 0.0


def _qualitative(entries: Sequence[RevealedJudgment]) -> tuple[QualitativeArmMetrics, ...]:
    return tuple(
        QualitativeArmMetrics(
            arm=arm,
            count=len(judgments := [entry.judgment for entry in entries if entry.arm is arm]),
            decision_quality=_mean(judgments, "decision_quality"),
            evidence_use=_mean(judgments, "evidence_use"),
            limitations_quality=_mean(judgments, "limitations_quality"),
            arm_guess_accuracy=(
                sum(item.arm_guess is arm for item in judgments) / len(judgments)
                if judgments
                else 0.0
            ),
            guess_confidence=_mean(judgments, "guess_confidence"),
        )
        for arm in Arm
    )


def _objective_report(
    manifest: DataAccessManifest,
    records: Sequence[tuple[str, RepetitionRun, ArmRun, PublicBlindOutput]],
    repetition_runs: Sequence[RepetitionRun],
) -> DataAccessReport:
    metrics = []
    for _opaque_id, _pair, arm_run, _output in records:
        decision = _decision(arm_run)
        sql_attempts = tuple(call.sql_attempt for call in arm_run.tool_calls)
        evaluation = evaluate_decision(decision, manifest, sql_attempts, arm=arm_run.arm)
        replay = replay_sql_evidence(manifest, sql_attempts, cited_tool_evidence_ids(decision))
        metrics.append(
            summarize_arm(
                {
                    "arm": arm_run.arm,
                    "repetition_id": arm_run.repetition_id,
                    "repetition_number": arm_run.repetition_number,
                    "elapsed_seconds": arm_run.elapsed_seconds,
                    "model_calls": arm_run.model_calls,
                    "sql_attempts": sql_attempts,
                },
                manifest=manifest,
                evaluation=evaluation,
                sql_replays=replay,
            )
        )
    return build_experiment_report(metrics, repetition_runs=repetition_runs, manifest=manifest)


def reveal_blind(run_root: Path, bundle: Path) -> tuple[RevealMap, RevealedReport]:
    """Reveal official labels and join frozen qualitative and private objective evidence."""
    run_manifest = _read_run_manifest(run_root)
    repetition_runs = validate_run_root(run_manifest, run_root)
    manifest = _read_manifest(bundle)
    records = _validated_source(run_root, manifest)
    _validated_outputs(bundle, manifest)
    frozen = _read_frozen(bundle, manifest)
    by_id = {record[0]: record for record in records}
    reveal_map = RevealMap(
        version=BLIND_VERSION,
        manifest_digest=_manifest_digest(manifest),
        entries=tuple(
            RevealEntry(
                opaque_id=opaque_id,
                repetition_number=by_id[opaque_id][1].repetition_number,
                repetition_id=by_id[opaque_id][1].repetition_id,
                arm=by_id[opaque_id][2].arm,
            )
            for opaque_id in manifest.ordered_opaque_ids
        ),
    )
    entries = {entry.opaque_id: entry for entry in reveal_map.entries}
    revealed = tuple(
        RevealedJudgment(
            opaque_id=judgment.opaque_id,
            repetition_number=entries[judgment.opaque_id].repetition_number,
            repetition_id=entries[judgment.opaque_id].repetition_id,
            arm=entries[judgment.opaque_id].arm,
            judgment=judgment,
        )
        for judgment in frozen.judgments
    )
    report = RevealedReport(
        version=BLIND_VERSION,
        raw_judgments=revealed,
        qualitative_metrics=_qualitative(revealed),
        objective_report=_objective_report(run_manifest.input_manifest, records, repetition_runs),
    )
    destination = bundle / REVEAL_DIRECTORY
    if destination.exists():
        raise FileExistsError(destination)
    stage = _stage(destination)
    published = False
    try:
        _write(stage / REVEAL_MAP_FILENAME, reveal_map)
        _write(stage / REVEALED_REPORT_FILENAME, report)
        if RevealMap.model_validate_json((stage / REVEAL_MAP_FILENAME).read_text()) != reveal_map:
            raise ValueError("staged reveal map is invalid")
        if RevealedReport.model_validate_json(
            (stage / REVEALED_REPORT_FILENAME).read_text()
        ) != report:
            raise ValueError("staged revealed report is invalid")
        _publish(stage, destination)
        published = True
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)
    return reveal_map, report
