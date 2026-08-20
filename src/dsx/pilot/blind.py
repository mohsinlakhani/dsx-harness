"""File-backed blind export, frozen judgment, and post-freeze reveal workflow."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .models import (
    Arm,
    ArmComparativeRatings,
    ArmGuessResult,
    ArmOutcome,
    AttemptTerminalStatus,
    BlindExclusion,
    BlindJudgment,
    BlindManifest,
    BlindOutputDigest,
    FrozenJudgments,
    OutcomeKind,
    PacketUptakeRatings,
    PairOutcomeKind,
    PairSummary,
    PilotContract,
    PublicBlindOutput,
    RevealedJudgment,
    RevealedReport,
    RevealEntry,
    RevealMap,
)

MANIFEST_FILENAME = "manifest.json"
FROZEN_JUDGMENTS_FILENAME = "frozen_judgments.json"
REVEAL_MAP_FILENAME = "reveal_map.json"
REVEALED_REPORT_FILENAME = "revealed_report.json"
REVEAL_DIRECTORY = "reveal"
BLIND_VERSION = "blind-v1"
OPAQUE_ID_LENGTH = 32
_OPAQUE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")

OpaqueIdFactory = Callable[[int, int, str, Arm], str]
ArtifactWriter = Callable[[Path, PilotContract], None]
DirectoryPublisher = Callable[[Path, Path], None]


def opaque_id_for(blind_seed: int, pair_number: int, pair_id: str, arm: Arm) -> str:
    """Return the fixed-length opaque identifier for one terminal arm artifact."""
    material = f"{BLIND_VERSION}\0{blind_seed}\0{pair_number}\0{pair_id}\0{arm.value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:OPAQUE_ID_LENGTH]


def _validate_opaque_id(opaque_id: str) -> str:
    if not _OPAQUE_ID_PATTERN.fullmatch(opaque_id):
        raise ValueError("opaque ID must be exactly 32 lowercase hexadecimal characters")
    if opaque_id in {MANIFEST_FILENAME, FROZEN_JUDGMENTS_FILENAME, REVEAL_DIRECTORY}:
        raise ValueError("opaque ID is reserved")
    return opaque_id


def _read_pair_summaries(run_root: Path) -> list[PairSummary]:
    pairs = [
        PairSummary.model_validate_json(path.read_text(encoding="utf-8"))
        for path in run_root.glob("pair-*/pair_summary.json")
    ]
    return sorted(pairs, key=lambda pair: (pair.pair_number, pair.pair_id))


def _final_outcome_by_arm(pair: PairSummary) -> dict[Arm, ArmOutcome]:
    outcomes: dict[Arm, ArmOutcome] = {}
    for outcome in pair.attempts[-1].outcomes:
        existing = outcomes.get(outcome.arm)
        if existing is None or outcome.request_number >= existing.request_number:
            outcomes[outcome.arm] = outcome
    return outcomes


def _final_completed_outcomes(pair: PairSummary) -> tuple[ArmOutcome, ArmOutcome] | None:
    if pair.outcome_kind is not PairOutcomeKind.complete:
        return None
    final_attempt = pair.attempts[-1]
    if final_attempt.terminal_status is not AttemptTerminalStatus.complete:
        return None
    final_by_arm = _final_outcome_by_arm(pair)
    candidates = tuple(final_by_arm.get(arm) for arm in Arm)
    if any(outcome is None for outcome in candidates):
        return None
    packet_off, packet_on = candidates
    assert packet_off is not None
    assert packet_on is not None
    if (
        packet_off.outcome_kind is not OutcomeKind.completed
        or packet_off.parsed_decision is None
        or packet_on.outcome_kind is not OutcomeKind.completed
        or packet_on.parsed_decision is None
    ):
        return None
    return packet_off, packet_on


def _exclusion_reason(pair: PairSummary) -> str | None:
    if pair.outcome_kind is PairOutcomeKind.infra_incomplete:
        return "infra_incomplete"
    final_attempt = pair.attempts[-1]
    if final_attempt.terminal_status is not AttemptTerminalStatus.complete:
        return final_attempt.terminal_status.value
    final_by_arm = _final_outcome_by_arm(pair)
    for arm in Arm:
        outcome = final_by_arm.get(arm)
        if outcome is None:
            return "missing_final_outcome"
        if outcome.outcome_kind is not OutcomeKind.completed or outcome.parsed_decision is None:
            return outcome.outcome_kind.value
    return None


def _eligible_records(
    run_root: Path, blind_seed: int, opaque_id_factory: OpaqueIdFactory
) -> tuple[list[tuple[str, int, str, Arm, PublicBlindOutput]], tuple[BlindExclusion, ...]]:
    outputs: list[tuple[str, int, str, Arm, PublicBlindOutput]] = []
    exclusions: Counter[str] = Counter()
    for pair in _read_pair_summaries(run_root):
        final = _final_completed_outcomes(pair)
        if final is None:
            reason = _exclusion_reason(pair)
            # Every invalid final pair has a reason under the validated ledger contract.
            if reason is not None:  # pragma: no branch
                exclusions[reason] += 1
            continue
        for arm, outcome in zip(Arm, final, strict=True):
            opaque_id = opaque_id_factory(blind_seed, pair.pair_number, pair.pair_id, arm)
            _validate_opaque_id(opaque_id)
            # _final_completed_outcomes admits only completed decisions. Keep a
            # fail-closed check if an unchecked object ever crosses this boundary.
            if outcome.parsed_decision is None:  # pragma: no cover
                raise ValueError("eligible completed outcome has no parsed decision")
            outputs.append(
                (
                    opaque_id,
                    pair.pair_number,
                    pair.pair_id,
                    arm,
                    PublicBlindOutput(opaque_id=opaque_id, decision=outcome.parsed_decision),
                )
            )
    if len({record[0] for record in outputs}) != len(outputs):
        raise ValueError("opaque ID collision")
    return outputs, tuple(
        BlindExclusion(reason=reason, count=count) for reason, count in sorted(exclusions.items())
    )


def _write_contract_exclusively(path: Path, contract: PilotContract) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _stage_directory(destination: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))


def _publish_directory(stage: Path, destination: Path) -> None:
    """Exclusively publish immutable sibling contents via an atomic directory symlink."""
    os.symlink(stage.name, destination, target_is_directory=True)


def _committed_stage(stage: Path, destination: Path) -> bool:
    """Whether the exclusive public link was committed to this exact private stage."""
    return destination.is_symlink() and os.readlink(destination) == stage.name


def export_blind(
    run_root: Path,
    destination: Path,
    *,
    blind_seed: int,
    opaque_id_factory: OpaqueIdFactory = opaque_id_for,
    artifact_writer: ArtifactWriter = _write_contract_exclusively,
    directory_publisher: DirectoryPublisher = _publish_directory,
) -> BlindManifest:
    """Publish opaque eligible decisions in a new exclusive blind bundle directory."""
    records, manifest = _manifest_from_source(run_root, blind_seed, opaque_id_factory)
    outputs = [(record[0], record[4]) for record in records]
    stage = _stage_directory(destination)
    published = False
    try:
        artifact_writer(stage / MANIFEST_FILENAME, manifest)
        for opaque_id, output in outputs:
            artifact_writer(stage / f"{opaque_id}.json", output)
        _validated_public_outputs(stage, manifest)
        directory_publisher(stage, destination)
        published = True
    finally:
        if not (published or _committed_stage(stage, destination)) and stage.exists():
            shutil.rmtree(stage)
    return manifest


def _manifest_from_source(
    run_root: Path, blind_seed: int, opaque_id_factory: OpaqueIdFactory
) -> tuple[list[tuple[str, int, str, Arm, PublicBlindOutput]], BlindManifest]:
    """Deterministically derive every public commitment from the private source run."""
    records, exclusions = _eligible_records(run_root, blind_seed, opaque_id_factory)
    outputs = [(record[0], record[4]) for record in records]
    outputs.sort(key=lambda item: item[0])
    random.Random(blind_seed).shuffle(outputs)
    return records, BlindManifest(
        version=BLIND_VERSION,
        blind_seed=blind_seed,
        ordered_opaque_ids=tuple(opaque_id for opaque_id, _ in outputs),
        eligible_count=len(outputs),
        exclusions=exclusions,
        output_digests=tuple(
            BlindOutputDigest(opaque_id=opaque_id, digest=_output_digest(output))
            for opaque_id, output in outputs
        ),
    )


def _validated_source_records(
    run_root: Path,
    manifest: BlindManifest,
    opaque_id_factory: OpaqueIdFactory = opaque_id_for,
) -> list[tuple[str, int, str, Arm, PublicBlindOutput]]:
    """Return source records only when every public manifest commitment recomputes exactly."""
    records, expected = _manifest_from_source(run_root, manifest.blind_seed, opaque_id_factory)
    if manifest != expected:
        raise ValueError("blind manifest does not match verified source run")
    return records


def _read_manifest(bundle: Path) -> BlindManifest:
    content = (bundle / MANIFEST_FILENAME).read_text(encoding="utf-8")
    return BlindManifest.model_validate_json(content)


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest_digest(manifest: BlindManifest) -> str:
    return _canonical_digest(manifest.model_dump(mode="json"))


def _output_digest(output: PublicBlindOutput) -> str:
    return _canonical_digest(output.model_dump(mode="json"))


def _validated_public_outputs(
    bundle: Path, manifest: BlindManifest
) -> dict[str, PublicBlindOutput]:
    outputs: dict[str, PublicBlindOutput] = {}
    for commitment in manifest.output_digests:
        path = bundle / f"{commitment.opaque_id}.json"
        output = PublicBlindOutput.model_validate_json(path.read_text(encoding="utf-8"))
        if output.opaque_id != commitment.opaque_id or _output_digest(output) != commitment.digest:
            raise ValueError("public output digest does not match manifest")
        outputs[output.opaque_id] = output
    return outputs


def _judgments_digest(judgments: tuple[BlindJudgment, ...]) -> str:
    return _canonical_digest([judgment.model_dump(mode="json") for judgment in judgments])


def _validated_ordered_judgments(
    manifest: BlindManifest, raw_judgments: list[BlindJudgment | dict[str, object]]
) -> tuple[BlindJudgment, ...]:
    judgments = tuple(
        BlindJudgment.model_validate(
            judgment.model_dump(mode="json")
            if isinstance(judgment, BlindJudgment)
            else judgment
        )
        for judgment in raw_judgments
    )
    ids = [judgment.opaque_id for judgment in judgments]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate opaque judgment IDs")
    expected = set(manifest.ordered_opaque_ids)
    actual = set(ids)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise ValueError("missing opaque judgment IDs")
    if extra:
        raise ValueError("extra opaque judgment IDs")
    by_id = {judgment.opaque_id: judgment for judgment in judgments}
    return tuple(by_id[opaque_id] for opaque_id in manifest.ordered_opaque_ids)


def freeze_judgments(
    bundle: Path,
    raw_judgments: list[BlindJudgment | dict[str, object]],
    *,
    run_root: Path,
) -> FrozenJudgments:
    """Validate, canonicalize, and exclusively freeze the complete judgment set."""
    destination = bundle / FROZEN_JUDGMENTS_FILENAME
    if destination.exists():
        raise FileExistsError(destination)
    manifest = _read_manifest(bundle)
    _validated_source_records(run_root, manifest)
    _validated_public_outputs(bundle, manifest)
    judgments = _validated_ordered_judgments(manifest, raw_judgments)
    frozen = FrozenJudgments(
        manifest_digest=_manifest_digest(manifest),
        judgments_digest=_judgments_digest(judgments),
        judgments=judgments,
    )
    _write_contract_exclusively(destination, frozen)
    return frozen


def _read_validated_frozen_judgments(bundle: Path, manifest: BlindManifest) -> FrozenJudgments:
    frozen = FrozenJudgments.model_validate_json(
        (bundle / FROZEN_JUDGMENTS_FILENAME).read_text(encoding="utf-8")
    )
    if frozen.manifest_digest != _manifest_digest(manifest):
        raise ValueError("frozen judgments manifest digest does not match manifest")
    ordered = _validated_ordered_judgments(manifest, list(frozen.judgments))
    if ordered != frozen.judgments or frozen.judgments_digest != _judgments_digest(ordered):
        raise ValueError("frozen judgments digest does not match canonical judgments")
    return frozen


def _mean(judgments: list[BlindJudgment], attribute: str) -> float:
    if not judgments:
        return 0.0
    return sum(int(getattr(judgment, attribute)) for judgment in judgments) / len(judgments)


def _revealed_report(
    reveal_map: RevealMap, frozen: FrozenJudgments
) -> RevealedReport:
    entries = {entry.opaque_id: entry for entry in reveal_map.entries}
    raw = tuple(
        RevealedJudgment(
            opaque_id=judgment.opaque_id,
            pair_number=entries[judgment.opaque_id].pair_number,
            pair_id=entries[judgment.opaque_id].pair_id,
            arm=entries[judgment.opaque_id].arm,
            judgment=judgment,
        )
        for judgment in frozen.judgments
    )
    comparative: list[ArmComparativeRatings] = []
    uptake: list[PacketUptakeRatings] = []
    guesses: list[ArmGuessResult] = []
    for arm in Arm:
        arm_judgments = [item.judgment for item in raw if item.arm is arm]
        count = len(arm_judgments)
        comparative.append(
            ArmComparativeRatings(
                arm=arm,
                count=count,
                decision_quality=_mean(arm_judgments, "decision_quality"),
                evidence_use=_mean(arm_judgments, "evidence_use"),
                limitations_quality=_mean(arm_judgments, "limitations_quality"),
                metric_reasoning=_mean(arm_judgments, "metric_reasoning"),
                split_strategy=_mean(arm_judgments, "split_strategy"),
                leakage_row_id_avoidance=_mean(arm_judgments, "leakage_row_id_avoidance"),
                limitations=_mean(arm_judgments, "limitations"),
                overall_recommendation_quality=_mean(
                    arm_judgments, "overall_recommendation_quality"
                ),
            )
        )
        uptake.append(
            PacketUptakeRatings(
                arm=arm,
                count=count,
                exact_prevalence_recognition=_mean(arm_judgments, "exact_prevalence_recognition"),
                majority_baseline_recognition=_mean(arm_judgments, "majority_baseline_recognition"),
                citation_use=_mean(arm_judgments, "citation_use"),
            )
        )
        correct = sum(judgment.packet_guess is arm for judgment in arm_judgments)
        guesses.append(
            ArmGuessResult(
                arm=arm,
                count=count,
                correct_count=correct,
                accuracy=correct / count if count else 0.0,
                mean_confidence=_mean(arm_judgments, "guess_confidence"),
            )
        )
    return RevealedReport(
        raw_judgments=raw,
        comparative_ratings=tuple(comparative),
        packet_uptake_diagnostics=tuple(uptake),
        arm_guess_results=tuple(guesses),
    )


def reveal_blind(
    run_root: Path,
    bundle: Path,
    *,
    opaque_id_factory: OpaqueIdFactory = opaque_id_for,
    artifact_writer: ArtifactWriter = _write_contract_exclusively,
    directory_publisher: DirectoryPublisher = _publish_directory,
) -> tuple[RevealMap, RevealedReport]:
    """Reveal official treatment labels only after validating a frozen judgment gate."""
    reveal_destination = bundle / REVEAL_DIRECTORY
    manifest = _read_manifest(bundle)
    records = _validated_source_records(run_root, manifest, opaque_id_factory)
    frozen = _read_validated_frozen_judgments(bundle, manifest)
    _validated_public_outputs(bundle, manifest)
    by_id = {record[0]: record for record in records}
    reveal_map = RevealMap(
        manifest_digest=_manifest_digest(manifest),
        entries=tuple(
            RevealEntry(
                opaque_id=opaque_id,
                pair_number=by_id[opaque_id][1],
                pair_id=by_id[opaque_id][2],
                arm=by_id[opaque_id][3],
            )
            for opaque_id in manifest.ordered_opaque_ids
        ),
    )
    report = _revealed_report(reveal_map, frozen)
    stage = _stage_directory(reveal_destination)
    published = False
    try:
        artifact_writer(stage / REVEAL_MAP_FILENAME, reveal_map)
        artifact_writer(stage / REVEALED_REPORT_FILENAME, report)
        if RevealMap.model_validate_json((stage / REVEAL_MAP_FILENAME).read_text()) != reveal_map:
            raise ValueError("staged reveal map failed validation")
        staged_report = RevealedReport.model_validate_json(
            (stage / REVEALED_REPORT_FILENAME).read_text()
        )
        if staged_report != report:
            raise ValueError("staged revealed report failed validation")
        directory_publisher(stage, reveal_destination)
        published = True
    finally:
        if not (published or _committed_stage(stage, reveal_destination)) and stage.exists():
            shutil.rmtree(stage)
    return reveal_map, report
