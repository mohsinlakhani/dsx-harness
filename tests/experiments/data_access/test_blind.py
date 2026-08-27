from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pytest

from dsx.experiments.data_access import blind
from dsx.experiments.data_access.blind import (
    BLIND_VERSION,
    FROZEN_JUDGMENTS_FILENAME,
    REVEAL_DIRECTORY,
    BlindJudgment,
    export_blind,
    freeze_judgments,
    reveal_blind,
)
from dsx.experiments.data_access.canonical import canonical_digest, canonical_json
from dsx.experiments.data_access.cli import DataAccessRunManifest
from dsx.experiments.data_access.execution import (
    ArmExecutionSpec,
    ArmRun,
    ExecutionOutcome,
    FunctionCall,
    ModelProviderError,
    ModelTransportError,
    RepetitionAttempt,
    RepetitionOutcome,
    RepetitionRun,
    ResponsesEnvelope,
    ScriptedResponsesClient,
    build_arm_specs,
    run_experiment,
    validate_arm_transcript,
)
from dsx.experiments.data_access.models import (
    Arm,
    CaseConfig,
    DatasetFormat,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest


def _decision() -> str:
    return canonical_json(
        {
            "primary_metric": "recall_at_5_percent",
            "supporting_metrics": ["precision_at_5_percent"],
            "review_budget_fraction": 0.05,
            "split_strategy": "stratified",
            "excluded_columns": ["row_id"],
            "reasoning": "Use the ranked review objective.",
            "limitations": ["synthetic"],
            "recommendation": "Validate before deployment.",
            "factual_claims": [
                {
                    "claim_id": "rows",
                    "statement": "The dataset has two rows.",
                    "predicate": "row_count",
                    "arguments": {},
                    "asserted_value": 2,
                    "evidence": [{"kind": "packet_json_pointer", "pointer": "/fact"}],
                }
            ],
            "narrative_claim_ids": ["rows"],
        }
    )


def _run_root(
    tmp_path: Path,
    *,
    refused_arm: Arm | None = None,
    discovery_arm: Arm | None = None,
    model_calls_per_arm: int = 16,
    transport_error_arm: Arm | None = None,
    infrastructure_error: ResponsesEnvelope | Exception | None = None,
) -> Path:
    source = tmp_path / "data.csv"
    source.write_text("row_id,label\na,0\nb,1\n", encoding="utf-8")
    manifest = prepare_manifest(
        case=CaseConfig(
            case_id="case",
            task_prompt="inspect",
            dataset_path=str(source),
            dataset_format=DatasetFormat.csv,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"fact": 2}),
        model=ModelConfig(model_identifier="offline", system_prompt="return JSON"),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=1),
            source="test",
            effective_date="2026-08-26",
        ),
        limits=ExperimentLimits(repetitions=1, model_calls_per_arm=model_calls_per_arm),
        database_path=tmp_path / "input.duckdb",
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    run_manifest = DataAccessRunManifest(
        run_manifest_version="data-access-run-manifest-v2",
        input_manifest=manifest,
        input_manifest_digest=canonical_digest(manifest),
        order_seed=7,
    )
    (run_root / "run_manifest.json").write_text(run_manifest.model_dump_json(), encoding="utf-8")
    seed = random.Random(run_manifest.order_seed).randrange(2**63)
    order = random.Random(seed).sample(list(Arm), k=len(Arm))
    script: list[ResponsesEnvelope | Exception] = []
    for arm in order:
        if arm is transport_error_arm:
            script.append(infrastructure_error or ModelTransportError("offline"))
        elif arm is refused_arm:
            script.append(ResponsesEnvelope(raw_envelope_json="{}", refusal="no"))
        elif arm is discovery_arm:
            function_call = FunctionCall(
                call_id="query-1",
                name="query_data",
                arguments_json=canonical_json({"sql": "SELECT count(*) AS n FROM dataset"}),
            )
            function_output = {
                "type": "function_call",
                "call_id": function_call.call_id,
                "name": function_call.name,
                "arguments": function_call.arguments_json,
            }
            script.extend(
                (
                    ResponsesEnvelope(
                        raw_envelope_json=canonical_json({"output": [function_output]}),
                        function_calls=(function_call,),
                        continuation_items_json=(canonical_json(function_output),),
                    ),
                    ResponsesEnvelope(raw_envelope_json="{}", decision_json=_decision()),
                )
            )
        else:
            script.append(ResponsesEnvelope(raw_envelope_json="{}", decision_json=_decision()))
    run_experiment(
        manifest,
        run_root=run_root,
        client=ScriptedResponsesClient(script),
        order_seed=run_manifest.order_seed,
    )
    return run_root


def _judgment(opaque_id: str, arm: Arm = Arm.dsx_packet) -> BlindJudgment:
    return BlindJudgment(
        opaque_id=opaque_id,
        decision_quality=4,
        evidence_use=3,
        limitations_quality=4,
        arm_guess=arm,
        guess_confidence=2,
    )


def test_blind_export_freeze_and_reveal_three_arm_repetition(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=9)

    assert manifest.version == BLIND_VERSION
    assert manifest.eligible_count == len(Arm) == 3
    assert len(manifest.ordered_opaque_ids) == 3
    public = (bundle / f"{manifest.ordered_opaque_ids[0]}.json").read_text()
    assert "packet_json_pointer" not in public
    assert "/fact" not in public
    assert all(arm.value not in public for arm in Arm)

    frozen = freeze_judgments(
        bundle,
        [
            _judgment(opaque_id, arm)
            for opaque_id, arm in zip(manifest.ordered_opaque_ids, Arm, strict=True)
        ],
        run_root=run_root,
    )
    assert (bundle / FROZEN_JUDGMENTS_FILENAME).is_file()
    assert len(frozen.judgments) == 3

    reveal_map, report = reveal_blind(run_root, bundle)
    assert (bundle / REVEAL_DIRECTORY).is_dir()
    assert {entry.arm for entry in reveal_map.entries} == set(Arm)
    assert {item.arm for item in report.qualitative_metrics} == set(Arm)
    assert len(report.raw_judgments) == 3
    assert len(report.objective_report.raw_arm_metrics) == 3
    assert len(report.objective_report.arm_contrasts) == 3


def test_only_complete_three_arm_repetitions_are_eligible(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path, refused_arm=Arm.packet_and_full_data)
    manifest = export_blind(run_root, tmp_path / "blind", blind_seed=9)

    assert manifest.eligible_count == 0
    assert manifest.exclusions == (RepetitionOutcome.complete.value,)


def test_freeze_requires_each_blind_output_once_and_reveal_is_exclusive(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=9)
    with pytest.raises(ValueError, match="missing"):
        freeze_judgments(bundle, [_judgment(manifest.ordered_opaque_ids[0])], run_root=run_root)
    with pytest.raises(ValueError, match="duplicate"):
        freeze_judgments(
            bundle,
            [_judgment(manifest.ordered_opaque_ids[0])] * 2
            + [_judgment(opaque_id) for opaque_id in manifest.ordered_opaque_ids[1:]],
            run_root=run_root,
        )
    with pytest.raises(FileNotFoundError):
        reveal_blind(run_root, bundle)
    freeze_judgments(
        bundle,
        [_judgment(opaque_id) for opaque_id in manifest.ordered_opaque_ids],
        run_root=run_root,
    )
    reveal_blind(run_root, bundle)
    with pytest.raises(FileExistsError):
        reveal_blind(run_root, bundle)


def test_blind_publication_and_freeze_commitments_reject_tampering(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=9)
    output_path = bundle / f"{manifest.ordered_opaque_ids[0]}.json"
    output = blind.PublicBlindOutput.model_validate_json(output_path.read_text(encoding="utf-8"))
    output_path.write_text(
        output.model_copy(update={"opaque_id": "different"}).model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="digest"):
        blind._validated_outputs(bundle, manifest)

    output_path.write_text(output.model_dump_json(), encoding="utf-8")
    frozen = freeze_judgments(
        bundle,
        [_judgment(opaque_id) for opaque_id in manifest.ordered_opaque_ids],
        run_root=run_root,
    )
    frozen_path = bundle / FROZEN_JUDGMENTS_FILENAME
    frozen_path.write_text(
        frozen.model_copy(update={"judgments_digest": "0" * 64}).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="frozen"):
        blind._read_frozen(bundle, manifest)


def test_repetition_contracts_and_opaque_ids_fail_closed() -> None:
    completed = ArmRun.model_construct(
        arm=Arm.dsx_packet, terminal_outcome=ExecutionOutcome.completed
    )
    incomplete = RepetitionRun.model_construct(
        outcome=RepetitionOutcome.infra_incomplete, attempts=()
    )
    assert blind._final_completed(incomplete) is None
    missing = RepetitionRun.model_construct(
        outcome=RepetitionOutcome.complete,
        attempts=(RepetitionAttempt.model_construct(arms=(completed,)),),
    )
    assert blind._final_completed(missing) is None
    partial = RepetitionRun.model_construct(
        outcome=RepetitionOutcome.complete,
        attempts=(
            RepetitionAttempt.model_construct(
                terminal_status=blind.AttemptStatus.complete,
                arms=(completed,),
            ),
        ),
    )
    assert blind._final_completed(partial) is None
    assert blind.opaque_id_for(1, 2, "repetition-002", Arm.dsx_packet) != blind.opaque_id_for(
        1, 2, "repetition-002", Arm.packet_and_full_data
    )


def test_run_root_validator_rejects_v1_layout_and_tampered_repetition(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    run_manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert len(blind.validate_run_root(run_manifest, run_root)) == 1
    repetition_dir = next(run_root.glob("repetition-*"))
    (repetition_dir / "repetition_run.json").rename(repetition_dir / "pair_run.json")
    with pytest.raises(ValueError, match="repetition records"):
        blind.validate_run_root(run_manifest, run_root)


@pytest.mark.parametrize("transport_error_arm", (Arm.dsx_packet, Arm.full_data))
@pytest.mark.parametrize(
    "infrastructure_error",
    (
        ModelTransportError("offline"),
        ModelProviderError("unavailable"),
        ResponsesEnvelope(status="failed"),
    ),
)
def test_run_root_validates_low_budget_infrastructure_retry_blocked_by_call_cap(
    tmp_path: Path,
    transport_error_arm: Arm,
    infrastructure_error: ResponsesEnvelope | Exception,
) -> None:
    run_root = _run_root(
        tmp_path,
        model_calls_per_arm=1,
        transport_error_arm=transport_error_arm,
        infrastructure_error=infrastructure_error,
    )
    run_manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    )

    repetitions = blind.validate_run_root(run_manifest, run_root)

    limited = next(
        run for run in repetitions[0].attempts[0].arms if run.arm is transport_error_arm
    )
    assert limited.terminal_outcome is ExecutionOutcome.model_call_limit


def test_fail_closed_model_and_private_record_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="eligible_count"):
        blind.BlindManifest(
            version=BLIND_VERSION,
            blind_seed=1,
            ordered_opaque_ids=("a",),
            eligible_count=0,
            output_digests=(blind.BlindOutputDigest(opaque_id="a", digest="a" * 64),),
        )
    with pytest.raises(ValueError, match="unique"):
        blind.BlindManifest(
            version=BLIND_VERSION,
            blind_seed=1,
            ordered_opaque_ids=("a", "a"),
            eligible_count=2,
            output_digests=(
                blind.BlindOutputDigest(opaque_id="a", digest="a" * 64),
                blind.BlindOutputDigest(opaque_id="a", digest="a" * 64),
            ),
        )
    with pytest.raises(ValueError, match="follow"):
        blind.BlindManifest(
            version=BLIND_VERSION,
            blind_seed=1,
            ordered_opaque_ids=("a",),
            eligible_count=1,
            output_digests=(blind.BlindOutputDigest(opaque_id="b", digest="a" * 64),),
        )
    with pytest.raises(ValueError, match="does not contain"):
        blind._decision(ArmRun.model_construct(decision_json=None))
    with pytest.raises(ValueError, match="not a JSON object"):
        blind._decision(ArmRun.model_construct(decision_json="[]"))

    arm = ArmRun.model_construct(
        arm=Arm.dsx_packet,
        terminal_outcome=ExecutionOutcome.completed,
        decision_json=_decision(),
    )
    repetition = RepetitionRun.model_construct(
        repetition_number=1,
        repetition_id="r",
        outcome=RepetitionOutcome.complete,
        attempts=(
                RepetitionAttempt.model_construct(
                    arms=(
                        arm,
                        arm.model_copy(update={"arm": Arm.full_data}),
                        arm.model_copy(update={"arm": Arm.packet_and_full_data}),
                    ),
                    terminal_status=blind.AttemptStatus.complete,
                ),
        ),
    )
    monkeypatch.setattr(blind, "_read_repetition_runs", lambda _root: (repetition,))
    monkeypatch.setattr(blind, "opaque_id_for", lambda *_args: "collision")
    with pytest.raises(ValueError, match="collision"):
        blind._records(tmp_path, 1)


def test_execution_combined_context_guards_and_packet_function_call(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    ).input_manifest
    packet, _full, combined = build_arm_specs(manifest)
    with pytest.raises(ValueError, match="packet_and_full_data requires"):
        ArmExecutionSpec.model_validate({**combined.model_dump(), "tools_json": ()})
    with pytest.raises(ValueError, match="packet_and_full_data must expose"):
        ArmExecutionSpec.model_validate(
            {**combined.model_dump(), "tools_json": (canonical_json({}),)}
        )

    arm_path = next(run_root.glob("repetition-*/attempts/*/arms/dsx_packet/arm_run.json"))
    packet_run = ArmRun.model_validate_json(arm_path.read_text(encoding="utf-8"))
    call = packet_run.model_calls[0]
    function_call = FunctionCall(
        call_id="query-1", name="query_data", arguments_json=canonical_json({"sql": "SELECT 1"})
    )
    changed_response = call.response.model_copy(update={"function_calls": (function_call,)})
    changed_call = call.model_copy(update={"response": changed_response})
    changed_run = packet_run.model_copy(update={"model_calls": (changed_call,)})
    with pytest.raises(ValueError, match="data tool call"):
        validate_arm_transcript(packet, changed_run)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest", "run ledger manifest"),
        ("extra-repetition", "repetition directories"),
        ("extra-root", "unexpected root"),
        ("extra-repetition-artifact", "repetition artifacts"),
        ("extra-attempt-artifact", "attempt artifacts"),
        ("extra-attempt", "attempt directories"),
        ("changed-attempt-summary", "attempt artifacts"),
        ("empty-infra-summary", "exhausted infrastructure evidence"),
        ("changed-attempt-start", "attempt artifacts"),
        ("changed-attempt-start-repetition-number", "attempt artifacts"),
        ("changed-attempt-start-repetition-id", "attempt artifacts"),
        ("changed-attempt-start-number", "attempt artifacts"),
        ("missing-arm", "arm directories"),
        ("wrong-spec", "arm specification"),
        ("wrong-arm", "arm summary"),
        ("missing-model", "model-call artifacts"),
        ("missing-request", "pre-call request artifacts"),
        ("wrong-model", "model call"),
        ("wrong-request", "pre-call request"),
    ],
)
def test_run_ledger_boundaries_fail_closed(
    tmp_path: Path, mutation: str, message: str
) -> None:
    parent = tmp_path / mutation
    parent.mkdir()
    run_root = _run_root(parent)
    run_manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    )
    repetition = next(run_root.glob("repetition-*"))
    attempt = next(repetition.glob("attempts/*"))
    if mutation == "manifest":
        run_manifest = run_manifest.model_copy(update={"order_seed": 8})
    elif mutation == "extra-repetition":
        (run_root / "repetition-2-repetition-002").mkdir()
    elif mutation == "extra-root":
        (run_root / "extra.json").write_text("{}", encoding="utf-8")
    elif mutation == "extra-repetition-artifact":
        (repetition / "extra.txt").write_text("extra", encoding="utf-8")
    elif mutation == "extra-attempt-artifact":
        (attempt / "extra.txt").write_text("extra", encoding="utf-8")
    elif mutation == "extra-attempt":
        (repetition / "attempts" / "attempt-extra").mkdir()
    elif mutation == "changed-attempt-summary":
        path = attempt / "repetition_attempt.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["attempt_id"] = "changed"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "empty-infra-summary":
        path = attempt / "repetition_attempt.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["arms"] = []
        payload["terminal_status"] = "infra_failure"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation.startswith("changed-attempt-start"):
        path = attempt / "attempt_start.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "changed-attempt-start":
            payload["arm_order"] = list(reversed(payload["arm_order"]))
        elif mutation == "changed-attempt-start-repetition-number":
            payload["repetition_number"] = 2
        elif mutation == "changed-attempt-start-repetition-id":
            payload["repetition_id"] = "tampered"
        else:
            payload["attempt_number"] = 2
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "missing-arm":
        shutil.rmtree(next(attempt.glob("arms/*")))
    elif mutation == "wrong-spec":
        path = next(attempt.glob("arms/*/arm_spec.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model_identifier"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "wrong-arm":
        path = next(attempt.glob("arms/*/arm_run.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["elapsed_seconds"] = 99.0
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "missing-model":
        next(attempt.glob("arms/*/model_calls/*.json")).unlink()
    elif mutation == "missing-request":
        next(attempt.glob("arms/*/model_requests/*.json")).unlink()
    elif mutation == "wrong-model":
        path = next(attempt.glob("arms/*/model_calls/*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["elapsed_seconds"] = 99.0
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path = next(attempt.glob("arms/*/model_requests/*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["request"]["instructions"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        blind.validate_run_root(run_manifest, run_root)


def test_validator_internal_repetition_commitments_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = _run_root(tmp_path)
    run_manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    )
    repetition = blind.validate_run_root(run_manifest, run_root)[0]
    attempt = repetition.attempts[0]
    original_read_runs = blind._read_repetition_runs
    original_read_contract = blind._read_contract

    monkeypatch.setattr(blind, "_read_repetition_runs", lambda _root: ())
    with pytest.raises(ValueError, match="repetition count"):
        blind.validate_run_root(run_manifest, run_root)
    monkeypatch.setattr(blind, "_read_repetition_runs", original_read_runs)

    bad_repetition = repetition.model_copy(update={"repetition_id": "bad"})
    monkeypatch.setattr(blind, "_read_repetition_runs", lambda _root: (bad_repetition,))
    with pytest.raises(ValueError, match="repetition identity"):
        blind.validate_run_root(run_manifest, run_root)
    monkeypatch.setattr(blind, "_read_repetition_runs", original_read_runs)

    def changed_run_record(path: Path, kind: object) -> object:
        if path.name == "repetition_run.json":
            return repetition.model_copy(update={"repetition_id": "changed"})
        return original_read_contract(path, kind)  # type: ignore[arg-type]

    monkeypatch.setattr(blind, "_read_contract", changed_run_record)
    with pytest.raises(ValueError, match="repetition summary"):
        blind.validate_run_root(run_manifest, run_root)
    monkeypatch.setattr(blind, "_read_contract", original_read_contract)

    changed_attempt = attempt.model_copy(update={"attempt_id": "bad"})
    changed_repetition = repetition.model_copy(update={"attempts": (changed_attempt,)})
    monkeypatch.setattr(blind, "_read_repetition_runs", lambda _root: (changed_repetition,))
    monkeypatch.setattr(
        blind,
        "_read_contract",
        lambda path, kind: (
            changed_repetition
            if path.name == "repetition_run.json"
            else original_read_contract(path, kind)
        ),
    )
    with pytest.raises(ValueError, match="attempt identity"):
        blind.validate_run_root(run_manifest, run_root)

    reordered_attempt = RepetitionAttempt.model_construct(
        **{**attempt.model_dump(), "arms": tuple(reversed(attempt.arms))}
    )
    reordered_repetition = RepetitionRun.model_construct(
        **{**repetition.model_dump(), "attempts": (reordered_attempt,)}
    )
    monkeypatch.setattr(blind, "_read_repetition_runs", lambda _root: (reordered_repetition,))
    monkeypatch.setattr(
        blind,
        "_read_contract",
        lambda path, kind: (
            reordered_repetition
            if path.name == "repetition_run.json"
            else reordered_attempt
            if path.name == "repetition_attempt.json"
            else original_read_contract(path, kind)
        ),
    )
    with pytest.raises(ValueError, match="execution order"):
        blind.validate_run_root(run_manifest, run_root)

    wrong_arm = attempt.arms[0].model_copy(update={"repetition_id": "bad"})
    identity_attempt = RepetitionAttempt.model_construct(
        **{**attempt.model_dump(), "arms": (wrong_arm, *attempt.arms[1:])}
    )
    identity_repetition = RepetitionRun.model_construct(
        **{**repetition.model_dump(), "attempts": (identity_attempt,)}
    )
    monkeypatch.setattr(blind, "_read_repetition_runs", lambda _root: (identity_repetition,))
    monkeypatch.setattr(
        blind,
        "_read_contract",
        lambda path, kind: (
            identity_repetition
            if path.name == "repetition_run.json"
            else identity_attempt
            if path.name == "repetition_attempt.json"
            else original_read_contract(path, kind)
        ),
    )
    with pytest.raises(ValueError, match="arm identity"):
        blind.validate_run_root(run_manifest, run_root)


def test_tool_and_arm_artifact_ledgers_fail_closed(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path, discovery_arm=Arm.full_data)
    run_manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert blind.validate_run_root(run_manifest, run_root)
    attempt = next(run_root.glob("repetition-*/attempts/*"))
    (next(attempt.glob("arms/*")) / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="arm artifacts"):
        blind.validate_run_root(run_manifest, run_root)
    (next(attempt.glob("arms/*")) / "extra.txt").unlink()

    tool_path = next(attempt.glob("arms/full_data/tool_calls/*.json"))
    original_tool = tool_path.read_text(encoding="utf-8")
    payload = json.loads(original_tool)
    payload["elapsed_seconds"] = 99.0
    tool_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="tool call"):
        blind.validate_run_root(run_manifest, run_root)
    tool_path.write_text(original_tool, encoding="utf-8")
    tool_path.unlink()
    with pytest.raises(ValueError, match="tool-call artifacts"):
        blind.validate_run_root(run_manifest, run_root)


def test_blind_commitment_and_staged_reveal_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = _run_root(tmp_path)
    bundle = tmp_path / "blind"
    manifest = export_blind(run_root, bundle, blind_seed=9)
    with pytest.raises(ValueError, match="private"):
        blind._validated_source(run_root, manifest.model_copy(update={"eligible_count": 0}))
    with pytest.raises(FileExistsError):
        export_blind(run_root, bundle, blind_seed=9)
    with pytest.raises(ValueError, match="extra"):
        blind._ordered_judgments(
            manifest,
            [_judgment(opaque_id) for opaque_id in manifest.ordered_opaque_ids]
            + [_judgment("extra")],
        )
    freeze_judgments(
        bundle,
        [_judgment(opaque_id) for opaque_id in manifest.ordered_opaque_ids],
        run_root=run_root,
    )
    with pytest.raises(FileExistsError):
        freeze_judgments(
            bundle,
            [_judgment(opaque_id) for opaque_id in manifest.ordered_opaque_ids],
            run_root=run_root,
        )

    original_write = blind._write

    def changed_map(path: Path, contract: object) -> None:
        original_write(path, contract)  # type: ignore[arg-type]
        if path.name == "reveal_map.json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["entries"] = []
            path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(blind, "_write", changed_map)
    with pytest.raises(ValueError, match="staged reveal map"):
        reveal_blind(run_root, bundle)
    assert not (bundle / REVEAL_DIRECTORY).exists()

    def changed_report(path: Path, contract: object) -> None:
        original_write(path, contract)  # type: ignore[arg-type]
        if path.name == "revealed_report.json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["raw_judgments"] = []
            path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(blind, "_write", changed_report)
    with pytest.raises(ValueError, match="staged revealed report"):
        reveal_blind(run_root, bundle)
    assert not (bundle / REVEAL_DIRECTORY).exists()

    failed_destination = tmp_path / "failed"
    monkeypatch.setattr(blind, "_write", original_write)
    monkeypatch.setattr(
        blind,
        "_publish",
        lambda *_args: (_ for _ in ()).throw(OSError("publication failed")),
    )
    with pytest.raises(OSError, match="publication failed"):
        export_blind(run_root, failed_destination, blind_seed=10)
    assert not failed_destination.exists()


def test_arm_artifact_validator_rejects_missing_directory(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    run_manifest = DataAccessRunManifest.model_validate_json(
        (run_root / "run_manifest.json").read_text(encoding="utf-8")
    )
    repetition = blind.validate_run_root(run_manifest, run_root)[0]
    arm_run = repetition.attempts[0].arms[0]
    spec = {spec.arm: spec for spec in build_arm_specs(run_manifest.input_manifest)}[arm_run.arm]
    with pytest.raises(ValueError, match="missing an arm"):
        blind._validate_arm_artifacts(tmp_path / "missing", arm_run, spec)
