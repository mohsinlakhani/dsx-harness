"""Tests for the file-backed opaque blind-evaluation workflow."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsx.pilot import blind
from dsx.pilot.blind import (
    FROZEN_JUDGMENTS_FILENAME,
    MANIFEST_FILENAME,
    REVEAL_DIRECTORY,
    REVEAL_MAP_FILENAME,
    REVEALED_REPORT_FILENAME,
    export_blind,
    freeze_judgments,
    opaque_id_for,
    reveal_blind,
)
from dsx.pilot.models import (
    AnalysisDecision,
    Arm,
    ArmOutcome,
    AttemptStart,
    AttemptSummary,
    AttemptTerminalStatus,
    BlindJudgment,
    BlindManifest,
    BlindOutputDigest,
    FrozenJudgments,
    Metric,
    OutcomeKind,
    PairOutcomeKind,
    PairSummary,
    PublicBlindOutput,
    RevealedReport,
    RevealMap,
)


def _decision(reasoning: str = "Rank candidates within the review budget.") -> AnalysisDecision:
    return AnalysisDecision(
        primary_metric=Metric.recall_at_5_percent,
        supporting_metrics=(Metric.precision_at_5_percent,),
        review_budget_fraction=0.05,
        split_strategy="stratified validation",
        excluded_columns=("row_id",),
        reasoning=reasoning,
        limitations=("Synthetic example",),
        recommendation="Use a ranking model.",
        packet_citations=("Fact: class distribution is 4,900 label-0 rows and 100 label-1 rows.",),
    )


def _outcome(
    arm: Arm,
    kind: OutcomeKind,
    attempt_number: int = 1,
    request_number: int = 1,
    reasoning: str = "Rank candidates within the review budget.",
) -> ArmOutcome:
    return ArmOutcome(
        arm=arm,
        outcome_kind=kind,
        request_digest=f"request-{arm.value}-{attempt_number}",
        common_projection_digest=f"common-{attempt_number}",
        attempt_number=attempt_number,
        request_number=request_number,
        parsed_decision=_decision(reasoning) if kind is OutcomeKind.completed else None,
        diagnostic="not usable" if kind is not OutcomeKind.completed else None,
    )


def _attempt(
    pair_number: int,
    pair_id: str,
    number: int,
    *outcomes: ArmOutcome,
    status: AttemptTerminalStatus = AttemptTerminalStatus.complete,
) -> AttemptSummary:
    return AttemptSummary(
        start=AttemptStart(
            pair_number=pair_number,
            pair_id=pair_id,
            attempt_number=number,
            attempt_id=f"attempt-{number}",
            arm_order=(Arm.packet_off, Arm.packet_on),
            common_projection_digest=f"common-{number}",
        ),
        outcomes=outcomes,
        terminal_status=status,
    )


def _persist_pair(
    root: Path,
    pair_number: int,
    pair_id: str,
    outcome_kind: PairOutcomeKind = PairOutcomeKind.complete,
    attempts: tuple[AttemptSummary, ...] | None = None,
) -> PairSummary:
    if attempts is None:
        attempts = (
            _attempt(
                pair_number,
                pair_id,
                1,
                _outcome(Arm.packet_off, OutcomeKind.completed),
                _outcome(Arm.packet_on, OutcomeKind.completed),
            ),
        )
    summary = PairSummary(
        pair_number=pair_number,
        pair_id=pair_id,
        outcome_kind=outcome_kind,
        attempts=attempts,
    )
    pair_dir = root / f"pair-{pair_number}-{pair_id}"
    pair_dir.mkdir(parents=True)
    (pair_dir / "pair_summary.json").write_text(summary.model_dump_json(indent=2))
    return summary


def _judgments(manifest_ids: tuple[str, ...]) -> list[BlindJudgment]:
    return [
        BlindJudgment(
            opaque_id=opaque_id,
            decision_quality=4,
            evidence_use=3,
            limitations_quality=2,
            packet_guess=Arm.packet_on,
            guess_confidence=5,
            metric_reasoning=4,
            split_strategy=3,
            leakage_row_id_avoidance=5,
            limitations=2,
            overall_recommendation_quality=4,
            exact_prevalence_recognition=5,
            majority_baseline_recognition=4,
            citation_use=3,
        )
        for opaque_id in manifest_ids
    ]


def test_export_uses_only_final_completed_attempt_and_public_artifacts_are_opaque(
    tmp_path: Path,
) -> None:
    """Exporting a first-attempt response would leak an invalid comparison unit."""
    run_root = tmp_path / "run"
    _persist_pair(
        run_root,
        10,
        "retry",
        attempts=(
            _attempt(
                10,
                "retry",
                1,
                _outcome(Arm.packet_off, OutcomeKind.completed, reasoning="first attempt only"),
                _outcome(Arm.packet_on, OutcomeKind.transport_error),
                _outcome(Arm.packet_on, OutcomeKind.transport_error, request_number=2),
                _outcome(Arm.packet_on, OutcomeKind.transport_error, request_number=3),
                status=AttemptTerminalStatus.infra_failure,
            ),
            _attempt(
                10,
                "retry",
                2,
                _outcome(
                    Arm.packet_off,
                    OutcomeKind.completed,
                    2,
                    reasoning="final packet off",
                ),
                _outcome(
                    Arm.packet_on,
                    OutcomeKind.completed,
                    2,
                    reasoning="final packet on",
                ),
            ),
        ),
    )
    _persist_pair(
        run_root,
        2,
        "infrastructure",
        PairOutcomeKind.infra_incomplete,
        (
            _attempt(
                2,
                "infrastructure",
                1,
                _outcome(Arm.packet_off, OutcomeKind.transport_error),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=3),
                status=AttemptTerminalStatus.infra_failure,
            ),
            _attempt(
                2,
                "infrastructure",
                2,
                _outcome(Arm.packet_off, OutcomeKind.transport_error, 2),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, 2, 2),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, 2, 3),
                status=AttemptTerminalStatus.infra_failure,
            ),
        ),
    )
    _persist_pair(
        run_root,
        11,
        "refusal",
        attempts=(
            _attempt(
                11,
                "refusal",
                1,
                _outcome(Arm.packet_off, OutcomeKind.refused),
                _outcome(Arm.packet_on, OutcomeKind.completed),
            ),
        ),
    )

    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=17)

    assert manifest.eligible_count == 2
    assert [(item.reason, item.count) for item in manifest.exclusions] == [
        ("infra_incomplete", 1),
        ("refused", 1),
    ]
    assert len(manifest.ordered_opaque_ids) == 2
    manifest_payload = json.loads((bundle / MANIFEST_FILENAME).read_text())
    assert set(manifest_payload) == {
        "version",
        "blind_seed",
        "ordered_opaque_ids",
        "eligible_count",
        "exclusions",
        "output_digests",
    }
    assert BlindManifest.model_validate(manifest_payload) == manifest
    for opaque_id in manifest.ordered_opaque_ids:
        assert len(opaque_id) == 32
        assert all(character in "0123456789abcdef" for character in opaque_id)
        assert (bundle / f"{opaque_id}.json").is_file()
        payload = (bundle / f"{opaque_id}.json").read_text()
        assert "packet_off" not in payload
        assert "packet_on" not in payload
        assert "request-" not in payload
        assert "common-" not in payload
        assert "retry" not in payload
        assert set(json.loads(payload)) == {"opaque_id", "decision"}
        assert PublicBlindOutput.model_validate_json(payload).opaque_id == opaque_id
    exported_reasoning = {
        PublicBlindOutput.model_validate_json(
            (bundle / f"{opaque_id}.json").read_text()
        ).decision.reasoning
        for opaque_id in manifest.ordered_opaque_ids
    }
    assert exported_reasoning == {"final packet off", "final packet on"}
    summary_text = (run_root / "pair-10-retry" / "pair_summary.json").read_text()
    assert PairSummary.model_validate_json(summary_text)
    assert (bundle / MANIFEST_FILENAME).is_file()


def test_export_is_seeded_exclusive_and_collision_safe(tmp_path: Path) -> None:
    """Changing a seed, reusing a destination, or colliding IDs must not publish ambiguity."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 2, "a")
    _persist_pair(run_root, 1, "b")

    first = export_blind(run_root, tmp_path / "first", blind_seed=4)
    same = export_blind(run_root, tmp_path / "same", blind_seed=4)
    changed = export_blind(run_root, tmp_path / "changed", blind_seed=5)

    assert first.ordered_opaque_ids == same.ordered_opaque_ids
    assert set(first.ordered_opaque_ids) != set(changed.ordered_opaque_ids)
    filenames = {path.name for path in (tmp_path / "first").iterdir()}
    assert {"packet_on.json", "packet_off.json"}.isdisjoint(filenames)
    with pytest.raises(FileExistsError):
        export_blind(run_root, tmp_path / "first", blind_seed=4)
    collision = tmp_path / "collision"
    with pytest.raises(ValueError, match="collision"):
        export_blind(run_root, collision, blind_seed=4, opaque_id_factory=lambda *_: "a" * 32)
    assert not collision.exists()


def test_seeded_permutation_starts_from_opaque_id_order_not_source_order(tmp_path: Path) -> None:
    """Inverting the public shuffle must not recover source pair/arm ordering."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 3, "three")
    _persist_pair(run_root, 1, "one")
    _persist_pair(run_root, 2, "two")

    def stable_id(_seed: int, number: int, pair_id: str, arm: Arm) -> str:
        return hashlib.sha256(f"{number}:{pair_id}:{arm.value}".encode()).hexdigest()[:32]

    seed = 123
    manifest = export_blind(
        run_root, tmp_path / "blind", blind_seed=seed, opaque_id_factory=stable_id
    )
    recovered = list(manifest.ordered_opaque_ids)
    permutation = list(range(len(recovered)))
    random.Random(seed).shuffle(permutation)
    pre_shuffle = ["" for _ in recovered]
    for shuffled_index, original_index in enumerate(permutation):
        pre_shuffle[original_index] = recovered[shuffled_index]

    source_order = [
        stable_id(seed, number, pair_id, arm)
        for number, pair_id in ((1, "one"), (2, "two"), (3, "three"))
        for arm in Arm
    ]
    assert pre_shuffle == sorted(source_order)
    assert pre_shuffle != source_order


@pytest.mark.parametrize(
    "opaque_id",
    ("../" + "a" * 29, "MANIFEST" + "a" * 24, "A" * 32, "a" * 31, "a" * 33),
)
def test_invalid_opaque_ids_abort_before_publication(tmp_path: Path, opaque_id: str) -> None:
    """Unsafe opaque IDs must never become artifact filenames."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    destination = tmp_path / "blind"
    with pytest.raises(ValueError, match="opaque ID"):
        export_blind(run_root, destination, blind_seed=1, opaque_id_factory=lambda *_: opaque_id)
    assert not destination.exists()


def test_export_failure_cleans_private_stage_and_allows_clean_retry(tmp_path: Path) -> None:
    """A partial write must not leave a usable destination or block a later clean export."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    destination = tmp_path / "blind"
    calls = 0

    def failing_writer(path: Path, contract: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        path.write_text(contract.model_dump_json())  # type: ignore[attr-defined]

    with pytest.raises(OSError, match="disk full"):
        export_blind(run_root, destination, blind_seed=1, artifact_writer=failing_writer)
    assert not destination.exists()
    assert not list(tmp_path.glob(".blind.stage-*"))
    assert export_blind(run_root, destination, blind_seed=1).eligible_count == 2


def test_exclusive_publication_preserves_dangling_links_and_competing_destinations(
    tmp_path: Path,
) -> None:
    """An exclusive publisher must never replace a pre-existing or racing destination."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    dangling = tmp_path / "dangling"
    os.symlink("absent-target", dangling)
    target = os.readlink(dangling)
    with pytest.raises(FileExistsError):
        export_blind(run_root, dangling, blind_seed=1)
    assert dangling.is_symlink() and os.readlink(dangling) == target
    assert not list(tmp_path.glob(".dangling.stage-*"))

    competing = tmp_path / "competing"

    def competing_publisher(stage: Path, destination: Path) -> None:
        destination.mkdir()
        blind._publish_directory(stage, destination)

    with pytest.raises(FileExistsError):
        export_blind(
            run_root,
            competing,
            blind_seed=1,
            directory_publisher=competing_publisher,
        )
    assert competing.is_dir() and not competing.is_symlink()
    assert not list(tmp_path.glob(".competing.stage-*"))

    bundle = tmp_path / "bundle"
    manifest = export_blind(run_root, bundle, blind_seed=2)
    freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    reveal_link = bundle / REVEAL_DIRECTORY
    os.symlink("missing-reveal", reveal_link)
    reveal_target = os.readlink(reveal_link)
    with pytest.raises(FileExistsError):
        reveal_blind(run_root, bundle)
    assert reveal_link.is_symlink() and os.readlink(reveal_link) == reveal_target
    assert not list(bundle.glob(".reveal.stage-*"))


def test_reveal_write_failure_cleans_stage_preserves_gate_and_can_retry(tmp_path: Path) -> None:
    """A reveal failure after its map write must not make labels partially public."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=1)
    freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    manifest_text = (bundle / MANIFEST_FILENAME).read_text()
    frozen_text = (bundle / FROZEN_JUDGMENTS_FILENAME).read_text()
    writes = 0

    def fail_after_map(path: Path, contract: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("reveal write failed")
        path.write_text(contract.model_dump_json())  # type: ignore[attr-defined]

    with pytest.raises(OSError, match="reveal write failed"):
        reveal_blind(run_root, bundle, artifact_writer=fail_after_map)
    assert not (bundle / REVEAL_DIRECTORY).exists()
    assert not list(bundle.glob(".reveal.stage-*"))
    assert (bundle / MANIFEST_FILENAME).read_text() == manifest_text
    assert (bundle / FROZEN_JUDGMENTS_FILENAME).read_text() == frozen_text
    assert reveal_blind(run_root, bundle)[0].entries


def test_publisher_raise_after_link_keeps_the_committed_private_target(tmp_path: Path) -> None:
    """Cleanup must not turn a committed exclusive link into a dangling no-clobber path."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    destination = tmp_path / "blind"

    def link_then_raise(stage: Path, final: Path) -> None:
        os.symlink(stage.name, final, target_is_directory=True)
        raise OSError("after commit")

    with pytest.raises(OSError, match="after commit"):
        export_blind(run_root, destination, blind_seed=1, directory_publisher=link_then_raise)
    assert destination.is_symlink() and (destination / MANIFEST_FILENAME).is_file()

    manifest = BlindManifest.model_validate_json((destination / MANIFEST_FILENAME).read_text())
    freeze_judgments(destination, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    with pytest.raises(OSError, match="after commit"):
        reveal_blind(run_root, destination, directory_publisher=link_then_raise)
    assert (destination / REVEAL_DIRECTORY).is_symlink()
    assert (destination / REVEAL_DIRECTORY / REVEAL_MAP_FILENAME).is_file()


def test_attempt_ledger_rejects_interleaving_and_request_sequences() -> None:
    """Attempt summaries reject non-runner arm grouping and retry numbering."""
    with pytest.raises(ValidationError, match="without interleaving"):
        _attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.transport_error),
            _outcome(Arm.packet_on, OutcomeKind.completed),
            _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
            status=AttemptTerminalStatus.infra_failure,
        )
    with pytest.raises(ValidationError, match="contiguous from one"):
        _attempt(1, "case", 1, _outcome(Arm.packet_off, OutcomeKind.completed, request_number=2))
    with pytest.raises(ValidationError, match="contiguous from one"):
        _attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.transport_error),
            _outcome(Arm.packet_off, OutcomeKind.completed, request_number=3),
        )
    with pytest.raises(ValidationError, match="requires request three"):
        _attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.transport_error),
            status=AttemptTerminalStatus.infra_failure,
        )


def test_attempt_terminal_statuses_reject_incoherent_groups() -> None:
    """Terminal status must agree with final outcome and execution position."""
    with pytest.raises(ValidationError, match="complete attempt"):
        _attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.transport_error),
            _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
            _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=3),
        )
    with pytest.raises(ValidationError, match="last executed arm"):
        _attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.completed),
            status=AttemptTerminalStatus.infra_failure,
        )


def test_attempt_ledger_rejects_executing_next_arm_after_exhaustion() -> None:
    """No arm may execute after an earlier arm exhausts its infrastructure retries."""
    with pytest.raises(ValidationError, match="only the last executed arm may exhaust"):
        AttemptSummary(
            start=AttemptStart(
                pair_number=1,
                pair_id="case",
                attempt_number=1,
                attempt_id="attempt-1",
                arm_order=(Arm.packet_off, Arm.packet_on),
                common_projection_digest="common-1",
            ),
            outcomes=(
                _outcome(Arm.packet_off, OutcomeKind.transport_error),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=3),
                _outcome(Arm.packet_on, OutcomeKind.completed),
            ),
            terminal_status=AttemptTerminalStatus.infra_failure,
        )


def test_pair_ledger_rejects_invalid_retry_transitions() -> None:
    """Pair terminals reject impossible retry histories independently of export eligibility."""
    complete = _attempt(
        1,
        "case",
        1,
        _outcome(Arm.packet_off, OutcomeKind.completed),
        _outcome(Arm.packet_on, OutcomeKind.completed),
    )
    retry_complete = _attempt(
        1,
        "case",
        2,
        _outcome(Arm.packet_off, OutcomeKind.completed, 2),
        _outcome(Arm.packet_on, OutcomeKind.completed, 2),
    )
    infra = _attempt(
        1,
        "case",
        1,
        _outcome(Arm.packet_off, OutcomeKind.transport_error),
        _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
        _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=3),
        status=AttemptTerminalStatus.infra_failure,
    )
    with pytest.raises(ValidationError, match="initial infrastructure"):
        PairSummary(
            pair_number=1,
            pair_id="case",
            outcome_kind=PairOutcomeKind.complete,
            attempts=(complete, retry_complete),
        )
    with pytest.raises(ValidationError, match="infra incomplete requires two"):
        PairSummary(
            pair_number=1,
            pair_id="case",
            outcome_kind=PairOutcomeKind.infra_incomplete,
            attempts=(infra,),
        )
    with pytest.raises(ValidationError, match="pair outcome kind"):
        PairSummary(
            pair_number=1,
            pair_id="case",
            outcome_kind=PairOutcomeKind.infra_incomplete,
            attempts=(infra, retry_complete),
        )


def test_reveal_rejects_tampered_public_or_source_decisions(tmp_path: Path) -> None:
    """Reveal joins only the exact evaluated decisions committed in the public manifest."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=1)
    freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    first = bundle / f"{manifest.ordered_opaque_ids[0]}.json"
    payload = json.loads(first.read_text())
    payload["decision"]["reasoning"] = "altered public decision"
    first.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="public output digest"):
        reveal_blind(run_root, bundle)

    fresh = tmp_path / "fresh"
    manifest = export_blind(run_root, fresh, blind_seed=1)
    freeze_judgments(fresh, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    source_path = run_root / "pair-1-case" / "pair_summary.json"
    source = json.loads(source_path.read_text())
    source["attempts"][-1]["outcomes"][0]["parsed_decision"]["reasoning"] = "altered source"
    source_path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="blind manifest does not match verified source run"):
        reveal_blind(run_root, fresh)


def test_reveal_rejects_missing_or_swapped_public_outputs_and_frozen_content(
    tmp_path: Path,
) -> None:
    """Every public record and frozen field is committed before treatment labels can emerge."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "one")
    _persist_pair(run_root, 2, "two")

    def bundle_at(name: str) -> tuple[Path, BlindManifest]:
        bundle = tmp_path / name
        manifest = export_blind(run_root, bundle, blind_seed=2)
        freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
        return bundle, manifest

    missing, manifest = bundle_at("missing")
    (missing / f"{manifest.ordered_opaque_ids[0]}.json").unlink()
    with pytest.raises(FileNotFoundError):
        reveal_blind(run_root, missing)

    swapped, manifest = bundle_at("swapped")
    first, second = (swapped / f"{opaque_id}.json" for opaque_id in manifest.ordered_opaque_ids[:2])
    first_text, second_text = first.read_text(), second.read_text()
    first.write_text(second_text)
    second.write_text(first_text)
    with pytest.raises(ValueError, match="public output digest"):
        reveal_blind(run_root, swapped)

    frozen_bundle, _ = bundle_at("frozen")
    frozen_path = frozen_bundle / FROZEN_JUDGMENTS_FILENAME
    frozen_payload = json.loads(frozen_path.read_text())
    frozen_payload["judgments"][0]["decision_quality"] = 1
    frozen_path.write_text(json.dumps(frozen_payload))
    with pytest.raises(ValueError, match="digest"):
        reveal_blind(run_root, frozen_bundle)


def test_pair_ledger_rejects_duplicate_requests_and_identity_mismatch(tmp_path: Path) -> None:
    """Malformed durable ledgers must be rejected before any blind artifact is published."""
    summary = _persist_pair(tmp_path / "run", 1, "case")
    duplicate = summary.model_dump(mode="json")
    duplicate["attempts"][0]["outcomes"].append(duplicate["attempts"][0]["outcomes"][0])
    with pytest.raises(ValidationError, match="duplicate"):
        PairSummary.model_validate(duplicate)
    mismatched = summary.model_dump(mode="json")
    mismatched["attempts"][0]["start"]["pair_id"] = "other"
    with pytest.raises(ValidationError, match="identity"):
        PairSummary.model_validate(mismatched)


def test_export_selects_the_highest_final_request_number_per_arm(tmp_path: Path) -> None:
    """Using ledger order instead of request number can export an obsolete arm result."""
    run_root = tmp_path / "run"
    _persist_pair(
        run_root,
        1,
        "unordered",
        attempts=(
            _attempt(
                1,
                "unordered",
                1,
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=1),
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
                _outcome(Arm.packet_off, OutcomeKind.completed, request_number=3),
                _outcome(Arm.packet_on, OutcomeKind.completed, request_number=1),
            ),
        ),
    )

    manifest = export_blind(run_root, tmp_path / "blind", blind_seed=2)

    assert manifest.eligible_count == 2


def test_freeze_validates_complete_scores_and_canonical_digest(tmp_path: Path) -> None:
    """A freeze that accepts altered, incomplete, or noncanonical judgments cannot gate reveal."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=3)
    judgments = _judgments(manifest.ordered_opaque_ids)

    with pytest.raises(ValueError, match="missing"):
        freeze_judgments(bundle, judgments[:-1], run_root=run_root)
    with pytest.raises(ValueError, match="extra"):
        freeze_judgments(bundle, judgments + [_judgments(("extra",))[0]], run_root=run_root)
    with pytest.raises(ValueError, match="duplicate"):
        freeze_judgments(bundle, judgments + [judgments[0]], run_root=run_root)
    with pytest.raises(ValidationError):
        freeze_judgments(bundle, [{"opaque_id": judgments[0].opaque_id}], run_root=run_root)
    for field in (
        "decision_quality",
        "evidence_use",
        "limitations_quality",
        "guess_confidence",
        "metric_reasoning",
        "split_strategy",
        "leakage_row_id_avoidance",
        "limitations",
        "overall_recommendation_quality",
        "exact_prevalence_recognition",
        "majority_baseline_recognition",
        "citation_use",
    ):
        for score in (0, 6):
            with pytest.raises(ValidationError):
                freeze_judgments(
                    bundle,
                    [judgments[0].model_copy(update={field: score})],
                    run_root=run_root,
                )
    malformed_guess = judgments[0].model_dump(mode="json")
    malformed_guess["packet_guess"] = "not_an_arm"
    with pytest.raises(ValidationError):
        freeze_judgments(bundle, [malformed_guess], run_root=run_root)

    frozen = freeze_judgments(bundle, list(reversed(judgments)), run_root=run_root)
    literal = json.dumps(
        [judgment.model_dump(mode="json") for judgment in judgments],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert frozen.judgments == tuple(judgments)
    assert frozen.judgments_digest == hashlib.sha256(literal).hexdigest()
    frozen_text = (bundle / FROZEN_JUDGMENTS_FILENAME).read_text()
    assert FrozenJudgments.model_validate_json(frozen_text) == frozen
    with pytest.raises(FileExistsError):
        freeze_judgments(
            bundle,
            [judgments[0].model_copy(update={"decision_quality": 1}), judgments[1]],
            run_root=run_root,
        )


def test_reveal_requires_valid_frozen_judgments_and_matching_source_ids(tmp_path: Path) -> None:
    """Reveal must reject absent or corrupted judgment gates and changed source identities."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=8)
    with pytest.raises(FileNotFoundError):
        reveal_blind(run_root, bundle)
    frozen = freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    tampered = frozen.model_copy(update={"judgments_digest": "0" * 64})
    (bundle / FROZEN_JUDGMENTS_FILENAME).write_text(tampered.model_dump_json())
    with pytest.raises(ValueError, match="digest"):
        reveal_blind(run_root, bundle)

    mismatch_root = tmp_path / "mismatch"
    _persist_pair(mismatch_root, 1, "changed")
    (bundle / FROZEN_JUDGMENTS_FILENAME).write_text(frozen.model_dump_json())
    with pytest.raises(ValueError, match="blind manifest does not match verified source run"):
        reveal_blind(mismatch_root, bundle)


def test_reveal_joins_official_arms_once_and_keeps_report_sections_separate(
    tmp_path: Path,
) -> None:
    """Mixing diagnostics into comparison or revealing twice corrupts interpretation."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=9)
    judgments = _judgments(manifest.ordered_opaque_ids)
    freeze_judgments(bundle, judgments, run_root=run_root)

    reveal_map, report = reveal_blind(run_root, bundle)

    assert {entry.arm for entry in reveal_map.entries} == {Arm.packet_off, Arm.packet_on}
    expected_arms = {opaque_id_for(9, 1, "case", arm): arm for arm in Arm}
    assert {entry.opaque_id: entry.arm for entry in reveal_map.entries} == expected_arms
    reveal_text = (bundle / REVEAL_DIRECTORY / REVEAL_MAP_FILENAME).read_text()
    report_text = (bundle / REVEAL_DIRECTORY / REVEALED_REPORT_FILENAME).read_text()
    assert RevealMap.model_validate_json(reveal_text) == reveal_map
    assert RevealedReport.model_validate_json(report_text) == report
    assert {item.arm for item in report.comparative_ratings} == {Arm.packet_off, Arm.packet_on}
    assert {item.arm for item in report.packet_uptake_diagnostics} == {
        Arm.packet_off,
        Arm.packet_on,
    }
    assert {item.arm for item in report.arm_guess_results} == {Arm.packet_off, Arm.packet_on}
    assert not hasattr(report.comparative_ratings[0], "exact_prevalence_recognition")
    assert report.raw_judgments[0].judgment.packet_guess is Arm.packet_on
    with pytest.raises(FileExistsError):
        reveal_blind(run_root, bundle)


def test_attempt_contracts_reject_each_identity_sequence_and_terminal_violation() -> None:
    """Every persisted attempt invariant must fail independently when corrupted."""
    with pytest.raises(ValidationError, match="both distinct arms"):
        AttemptStart(
            pair_number=1,
            pair_id="case",
            attempt_number=1,
            attempt_id="attempt",
            arm_order=(Arm.packet_off, Arm.packet_off),
            common_projection_digest="common-1",
        )

    start = _attempt(
        1,
        "case",
        1,
        _outcome(Arm.packet_off, OutcomeKind.completed),
        _outcome(Arm.packet_on, OutcomeKind.completed),
    ).start
    mismatch_cases = (
        (
            _outcome(Arm.packet_off, OutcomeKind.completed, attempt_number=2),
            "attempt number",
        ),
        (
            _outcome(Arm.packet_off, OutcomeKind.completed).model_copy(
                update={"common_projection_digest": "different"}
            ),
            "common projection",
        ),
    )
    for outcome, message in mismatch_cases:
        with pytest.raises(ValidationError, match=message):
            AttemptSummary(
                start=start,
                outcomes=(outcome,),
                terminal_status=AttemptTerminalStatus.complete,
            )

    with pytest.raises(ValidationError, match="at most three"):
        _attempt(
            1,
            "case",
            1,
            *(
                _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=number)
                for number in range(1, 5)
            ),
            status=AttemptTerminalStatus.infra_failure,
        )
    with pytest.raises(ValidationError, match="must end its arm"):
        _attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.completed),
            _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
            status=AttemptTerminalStatus.infra_failure,
        )


def test_pair_contract_rejects_wrong_attempt_number_and_duplicate_attempt_id() -> None:
    """Retry histories must preserve ordered numbers and fresh attempt identifiers."""
    complete_two = _attempt(
        1,
        "case",
        2,
        _outcome(Arm.packet_off, OutcomeKind.completed, attempt_number=2),
        _outcome(Arm.packet_on, OutcomeKind.completed, attempt_number=2),
    )
    with pytest.raises(ValidationError, match="ordered from one"):
        PairSummary(
            pair_number=1,
            pair_id="case",
            outcome_kind=PairOutcomeKind.complete,
            attempts=(complete_two,),
        )

    infra = _attempt(
        1,
        "case",
        1,
        _outcome(Arm.packet_off, OutcomeKind.transport_error),
        _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=2),
        _outcome(Arm.packet_off, OutcomeKind.transport_error, request_number=3),
        status=AttemptTerminalStatus.infra_failure,
    )
    duplicate_id = complete_two.model_copy(
        update={
            "start": complete_two.start.model_copy(update={"attempt_id": infra.start.attempt_id})
        }
    )
    with pytest.raises(ValidationError, match="attempt IDs must be unique"):
        PairSummary(
            pair_number=1,
            pair_id="case",
            outcome_kind=PairOutcomeKind.complete,
            attempts=(infra, duplicate_id),
        )


def test_blind_manifest_rejects_count_duplicate_and_commitment_order_drift() -> None:
    """A public index must bind one unique ordered commitment to every output."""
    valid = BlindManifest(
        version="blind-v1",
        blind_seed=1,
        ordered_opaque_ids=("a", "b"),
        eligible_count=2,
        exclusions=(),
        output_digests=(
            BlindOutputDigest(opaque_id="a", digest="digest-a"),
            BlindOutputDigest(opaque_id="b", digest="digest-b"),
        ),
    )
    mutations = (
        ({"eligible_count": 1}, "eligible_count"),
        ({"ordered_opaque_ids": ("a", "a")}, "unique"),
        ({"output_digests": tuple(reversed(valid.output_digests))}, "opaque ID order"),
    )
    for update, message in mutations:
        with pytest.raises(ValidationError, match=message):
            BlindManifest.model_validate({**valid.model_dump(), **update})


def test_blind_defenses_handle_reserved_ids_and_malformed_in_memory_ledgers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive helpers must fail closed if upstream format invariants are bypassed."""
    monkeypatch.setattr(blind, "_OPAQUE_ID_PATTERN", re.compile(r".*\Z"))
    with pytest.raises(ValueError, match="reserved"):
        blind._validate_opaque_id(MANIFEST_FILENAME)

    off_request_two = _outcome(
        Arm.packet_off, OutcomeKind.completed, request_number=2, reasoning="newer"
    )
    off_request_one = _outcome(
        Arm.packet_off, OutcomeKind.completed, request_number=1, reasoning="older"
    )
    malformed_attempt = AttemptSummary.model_construct(
        start=_attempt(
            1,
            "case",
            1,
            _outcome(Arm.packet_off, OutcomeKind.completed),
            _outcome(Arm.packet_on, OutcomeKind.completed),
        ).start,
        outcomes=(off_request_two, off_request_one),
        terminal_status=AttemptTerminalStatus.complete,
    )
    malformed_pair = PairSummary.model_construct(
        pair_number=1,
        pair_id="case",
        outcome_kind=PairOutcomeKind.complete,
        attempts=(malformed_attempt,),
    )
    assert blind._final_outcome_by_arm(malformed_pair)[Arm.packet_off] == off_request_two

    wrong_terminal = PairSummary.model_construct(
        pair_number=1,
        pair_id="case",
        outcome_kind=PairOutcomeKind.complete,
        attempts=(
            malformed_attempt.model_copy(
                update={"terminal_status": AttemptTerminalStatus.infra_failure}
            ),
        ),
    )
    assert blind._final_completed_outcomes(wrong_terminal) is None
    assert blind._exclusion_reason(wrong_terminal) == "infra_failure"

    missing_arm_attempt = malformed_attempt.model_copy(
        update={
            "outcomes": (_outcome(Arm.packet_off, OutcomeKind.completed),),
            "terminal_status": AttemptTerminalStatus.complete,
        }
    )
    missing_arm = malformed_pair.model_copy(update={"attempts": (missing_arm_attempt,)})
    assert blind._final_completed_outcomes(missing_arm) is None
    assert blind._exclusion_reason(missing_arm) == "missing_final_outcome"

    valid_pair = PairSummary(
        pair_number=1,
        pair_id="case",
        outcome_kind=PairOutcomeKind.complete,
        attempts=(
            _attempt(
                1,
                "case",
                1,
                _outcome(Arm.packet_off, OutcomeKind.completed),
                _outcome(Arm.packet_on, OutcomeKind.completed),
            ),
        ),
    )
    assert blind._exclusion_reason(valid_pair) is None


def test_empty_blind_workflow_reveals_explicit_zero_count_sections(tmp_path: Path) -> None:
    """A run with no eligible pairs must still produce a valid, honest zero-count report."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=4)
    assert manifest.eligible_count == 0
    freeze_judgments(bundle, [], run_root=run_root)

    _, report = reveal_blind(run_root, bundle)

    assert all(item.count == 0 for item in report.comparative_ratings)
    assert all(item.count == 0 for item in report.packet_uptake_diagnostics)
    assert all(item.accuracy == 0.0 for item in report.arm_guess_results)


def test_reveal_rejects_a_frozen_manifest_binding_change(tmp_path: Path) -> None:
    """Judgments frozen for a different manifest must never unlock source labels."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=4)
    frozen = freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)
    (bundle / FROZEN_JUDGMENTS_FILENAME).write_text(
        frozen.model_copy(update={"manifest_digest": "0" * 64}).model_dump_json()
    )

    with pytest.raises(ValueError, match="manifest digest"):
        reveal_blind(run_root, bundle)


@pytest.mark.parametrize("corrupt", ["map", "report"])
def test_reveal_validates_staged_artifacts_before_publication(
    tmp_path: Path, corrupt: str
) -> None:
    """A writer that changes staged content must not publish a misleading reveal."""
    run_root = tmp_path / "run"
    _persist_pair(run_root, 1, "case")
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=4)
    freeze_judgments(bundle, _judgments(manifest.ordered_opaque_ids), run_root=run_root)

    def corrupting_writer(path: Path, contract: object) -> None:
        if corrupt == "map" and path.name == REVEAL_MAP_FILENAME:
            path.write_text(
                RevealMap(manifest_digest="different", entries=()).model_dump_json()
            )
            return
        if corrupt == "report" and path.name == REVEALED_REPORT_FILENAME:
            path.write_text(
                RevealedReport(
                    raw_judgments=(),
                    comparative_ratings=(),
                    packet_uptake_diagnostics=(),
                    arm_guess_results=(),
                ).model_dump_json()
            )
            return
        path.write_text(contract.model_dump_json())  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="staged reveal(ed report| map).*"):
        reveal_blind(run_root, bundle, artifact_writer=corrupting_writer)
    assert not (bundle / REVEAL_DIRECTORY).exists()
