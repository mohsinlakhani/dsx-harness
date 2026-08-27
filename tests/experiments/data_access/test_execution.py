from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from dsx.experiments.data_access.canonical import canonical_json, parse_canonical_json
from dsx.experiments.data_access.evaluation import provider_decision_schema
from dsx.experiments.data_access.execution import (
    ArmExecutionSpec,
    ArmRun,
    AttemptStatus,
    ExecutionOutcome,
    FunctionCall,
    ModelCallLedger,
    ModelProviderError,
    ModelTransportError,
    OpenAIResponsesClient,
    RepetitionAttempt,
    RepetitionOutcome,
    RepetitionRun,
    ResponsesEnvelope,
    ResponsesRequest,
    ScriptedResponsesClient,
    ToolCallLedger,
    Usage,
    _decision_response_format,
    _function_result_item,
    _initial_input,
    _is_infrastructure,
    _normalize_openai_response,
    _recorded_continuation,
    _request,
    _response_outcome,
    _usage_from_value,
    _valid_decision,
    _write,
    build_arm_specs,
    run_arm,
    run_experiment,
    run_repetition,
    validate_arm_transcript,
    validate_fairness,
)
from dsx.experiments.data_access.models import (
    Arm,
    CaseConfig,
    DataAccessManifest,
    DatasetFormat,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    SqlAttempt,
    SqlAttemptOutcome,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest
from dsx.experiments.data_access.sql_tool import QUERY_DATA_TOOL_SCHEMA


def _manifest(tmp_path: Path) -> DataAccessManifest:
    source = tmp_path / "source.csv"
    source.write_text("row_id,label,value\na,0,one\nb,1,two\n", encoding="utf-8")
    return prepare_manifest(
        case=CaseConfig(
            case_id="case",
            task_prompt="Recommend a plan.",
            dataset_path=str(source),
            dataset_format=DatasetFormat.csv,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"finding": {"id": "row_id"}}),
        model=ModelConfig(model_identifier="test", system_prompt="Return JSON."),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=1),
            source="test",
            effective_date="2026-08-26",
        ),
        database_path=tmp_path / "dataset.duckdb",
    )


def _decision() -> str:
    return canonical_json(
        {
            "primary_metric": "recall_at_5_percent",
            "supporting_metrics": ["precision_at_5_percent"],
            "review_budget_fraction": 0.05,
            "split_strategy": "stratified validation",
            "excluded_columns": ["row_id"],
            "reasoning": "The data is rare-event ranking.",
            "limitations": ["synthetic"],
            "recommendation": "rank cases",
            "factual_claims": [],
            "narrative_claim_ids": [],
        }
    )


def _reply(*, decision: str | None = None, call: FunctionCall | None = None) -> ResponsesEnvelope:
    output: list[dict[str, object]] = []
    if call is not None:
        output.append(
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments_json,
            }
        )
    return ResponsesEnvelope(
        response_id="response",
        status="completed",
        raw_envelope_json=canonical_json({"output": output}),
        usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
        function_calls=(call,) if call else (),
        decision_json=decision,
        continuation_items_json=tuple(canonical_json(item) for item in output),
    )


def test_three_arms_are_fair_and_data_arms_use_a_stateless_tool_loop(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    packet, full_data, combined = build_arm_specs(manifest)
    assert validate_fairness((packet, full_data, combined)) == packet.common_projection_digest
    client = ScriptedResponsesClient([_reply(decision=_decision())] * 3)
    repetition = run_repetition(
        manifest,
        run_root=tmp_path / "run",
        repetition_number=1,
        repetition_id="a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )
    assert repetition.outcome == "complete"
    runs = {run.arm: run for run in repetition.attempts[0].arms}
    assert set(runs) == set(Arm)
    assert all(run.terminal_outcome is ExecutionOutcome.completed for run in runs.values())
    assert all(
        request.store is False and not request.parallel_tool_calls for request in client.requests
    )
    packet_context = json.loads(
        json.loads(_initial_input(packet)[0])["content"][0]["text"]
    )["context"]
    full_context = json.loads(
        json.loads(_initial_input(full_data)[0])["content"][0]["text"]
    )["context"]
    combined_context = json.loads(
        json.loads(_initial_input(combined)[0])["content"][0]["text"]
    )["context"]
    assert packet.common_projection["evidence_protocol_version"] == "v1"
    assert "packet_json_pointer" in packet_context["evidence_protocol"]
    assert "exactly" in packet_context["evidence_protocol"]
    for convention in (
        "row_count",
        "class_count",
        "class_rate",
        "majority_baseline",
        "review_count",
        "missingness",
        "uniqueness",
        "likely_id",
        "recommended_exclusions",
        "excluded_column",
    ):
        assert convention in full_context["evidence_protocol"]
    assert "tool_call" in full_context["evidence_protocol"]
    assert "dataset" in full_context["evidence_protocol"]
    assert combined_context["dsx_packet"] == parse_canonical_json(packet.packet_json or "{}")
    assert combined_context["dataset"] == combined.dataset_descriptor
    assert "packet_json_pointer" in combined_context["evidence_protocol"]
    assert "tool_call" in combined_context["evidence_protocol"]
    attempt = tmp_path / "run" / "repetition-1-a" / "attempts" / "attempt-a"
    assert (attempt / "arms" / "dsx_packet" / "arm_spec.json").is_file()
    assert (attempt / "arms" / "packet_and_full_data" / "arm_spec.json").is_file()


def test_provider_exhaustion_restarts_a_fresh_repetition_and_records_all_attempts(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    client = ScriptedResponsesClient(
        [ModelTransportError("offline")] * 3
        + [_reply(decision=_decision())] * 3
    )
    repetition = run_repetition(
        manifest,
        run_root=tmp_path / "run",
        repetition_number=1,
        repetition_id="a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
        sleep=lambda _: None,
    )
    assert repetition.outcome == "complete"
    assert len(repetition.attempts) == 2
    assert repetition.attempts[0].terminal_status == "infra_failure"
    calls = repetition.attempts[0].arms[0].model_calls
    assert [call.model_call_number for call in calls] == [1, 2, 3]


def test_fairness_rejects_a_common_model_setting_difference(tmp_path: Path) -> None:
    packet, full_data, combined = build_arm_specs(_manifest(tmp_path))
    changed = ArmExecutionSpec.model_validate(
        {**full_data.model_dump(mode="python"), "max_output_tokens": 99}
    )
    try:
        validate_fairness((packet, changed, combined))
    except ValueError as error:
        assert "treatment" in str(error)
    else:  # pragma: no cover - assertion clearer than pytest context here
        raise AssertionError("fairness validation accepted model drift")


def test_exact_request_artifact_exists_before_the_client_can_return(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    request_path = (
        tmp_path
        / "run"
        / "repetition-1-a"
        / "attempts"
        / "attempt-a"
        / "arms"
        / "dsx_packet"
        / "model_requests"
        / "001-1.json"
    )

    class InspectingClient(ScriptedResponsesClient):
        def create(self, request: ResponsesRequest) -> ResponsesEnvelope:
            if not self.requests:
                assert request_path.is_file()
                persisted = json.loads(request_path.read_text(encoding="utf-8"))
                assert persisted["request"]["input_items_json"] == list(request.input_items_json)
            return super().create(request)

    client = InspectingClient([_reply(decision=_decision())] * 3)
    run_repetition(
        manifest,
        run_root=tmp_path / "run",
        repetition_number=1,
        repetition_id="a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )
    response_path = request_path.parent.parent / "model_calls" / "001-1.json"
    assert response_path.is_file()


def test_runner_uses_the_evaluator_strict_provider_schema() -> None:
    schema = _decision_response_format("data_access_decision")
    assert schema == provider_decision_schema("data_access_decision")
    assert schema["type"] == "json_schema" and schema["strict"] is True

    def verify_objects(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                assert value.get("type") == "object"
                assert value.get("additionalProperties") is False
                assert set(value.get("required", [])) == set(properties)
            for item in value.values():
                verify_objects(item)
        elif isinstance(value, list):
            for item in value:
                verify_objects(item)

    verify_objects(schema["schema"])


def test_arm_transcript_reconstruction_rejects_tampering(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    packet_spec, data_spec, combined_spec = build_arm_specs(manifest)
    function = FunctionCall(
        call_id="c",
        name="query_data",
        arguments_json=canonical_json({"sql": "SELECT count(*) AS n FROM dataset"}),
    )
    repetition = run_repetition(
        manifest,
        run_root=tmp_path / "transcript",
        repetition_number=1,
        repetition_id="p",
        client=ScriptedResponsesClient([_reply(decision=_decision())] * 3),
        order_seed=1,
        attempt_id_factory=iter(("a",)).__next__,
    )
    packet_run = next(run for run in repetition.attempts[0].arms if run.arm is Arm.dsx_packet)
    data_run, _ = _run_one_arm(
        tmp_path / "data-transcript",
        manifest,
        "full",
        [_reply(call=function), _reply(decision=_decision())],
    )
    validate_arm_transcript(packet_spec, packet_run)
    validate_arm_transcript(data_spec, data_run)
    combined_run = next(
        run for run in repetition.attempts[0].arms if run.arm is Arm.packet_and_full_data
    )
    validate_arm_transcript(combined_spec, combined_run)

    def rejects(spec: ArmExecutionSpec, run: ArmRun, text: str) -> None:
        with pytest.raises(ValueError, match=text):
            validate_arm_transcript(spec, run)

    with pytest.raises(ValueError, match="must be positive"):
        validate_arm_transcript(packet_spec, packet_run, model_calls_per_arm=0)
    with pytest.raises(ValueError, match="configured arm limit"):
        validate_arm_transcript(
            packet_spec,
            packet_run.model_copy(
                update={
                    "model_calls": (
                        packet_run.model_calls[0],
                        packet_run.model_calls[0].model_copy(
                            update={"model_call_number": 2, "provider_attempt_number": 2}
                        ),
                    )
                }
            ),
            model_calls_per_arm=1,
        )
    rejects(data_spec, data_run.model_copy(update={"arm": Arm.dsx_packet}), "crosses")
    rejects(
        data_spec, data_run.model_copy(update={"common_projection_digest": "0" * 64}), "projection"
    )
    first = packet_run.model_calls[0]
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={"model_calls": (first.model_copy(update={"model_call_number": 2}),)}
        ),
        "numbering",
    )
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={"model_calls": (first.model_copy(update={"provider_attempt_number": 2}),)}
        ),
        "retry numbering",
    )
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={
                "model_calls": (
                    first.model_copy(
                        update={
                            "request": first.request.model_copy(update={"max_output_tokens": 1})
                        }
                    ),
                )
            }
        ),
        "reconstructed",
    )
    data_call = data_run.model_calls[0]
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={
                "model_calls": (
                        data_call.model_copy(
                            update={"request": _request(data_spec, _initial_input(data_spec))}
                    ),
                )
            }
        ),
            "reconstructed",
    )
    rejects(
        data_spec,
        data_run.model_copy(
            update={
                "model_calls": (data_call.model_copy(update={"outcome": ExecutionOutcome.refused}),)
            }
        ),
        "non-completed",
    )
    assert data_call.response is not None
    bad_response = data_call.response.model_copy(update={"continuation_items_json": ()})
    rejects(
        data_spec,
        data_run.model_copy(
            update={
                "model_calls": (
                    data_call.model_copy(update={"response": bad_response}),
                    data_run.model_calls[1],
                )
            }
        ),
        "continuation",
    )
    rejects(data_spec, data_run.model_copy(update={"tool_calls": ()}), "missing")
    tool = data_run.tool_calls[0]
    rejects(
        packet_spec,
        packet_run.model_copy(update={"tool_calls": (tool,)}),
        "packet arm",
    )
    rejects(
        data_spec,
        data_run.model_copy(
            update={"tool_calls": (tool.model_copy(update={"tool_call_number": 2}),)}
        ),
        "numbering",
    )
    rejects(
        data_spec,
        data_run.model_copy(
            update={"tool_calls": (tool.model_copy(update={"model_call_number": 2}),)}
        ),
        "wrong model",
    )
    rejects(
        data_spec,
        data_run.model_copy(
            update={
                "tool_calls": (
                    tool.model_copy(
                        update={"function_call": function.model_copy(update={"call_id": "other"})}
                    ),
                )
            }
        ),
        "do not match",
    )
    rejects(
        data_spec,
        data_run.model_copy(
            update={"tool_calls": (tool, tool.model_copy(update={"tool_call_number": 2}))}
        ),
        "extra tool",
    )
    duplicate = first.model_copy(update={"model_call_number": 2})
    rejects(
        packet_spec,
        packet_run.model_copy(update={"model_calls": (first, duplicate)}),
        "extra request",
    )
    final_infra = first.model_copy(
        update={"outcome": ExecutionOutcome.transport_error, "response": None}
    )
    exhausted_infra = final_infra.model_copy(
        update={"model_call_number": 3, "provider_attempt_number": 3}
    )
    validate_arm_transcript(
        packet_spec,
        packet_run.model_copy(
            update={
                "model_calls": (
                    final_infra,
                    final_infra.model_copy(
                        update={"model_call_number": 2, "provider_attempt_number": 2}
                    ),
                    exhausted_infra,
                ),
                "terminal_outcome": ExecutionOutcome.transport_error,
                "usage": Usage(),
                "decision_json": None,
            }
        ),
    )
    rejects(
        packet_spec,
        packet_run.model_copy(update={"usage": Usage(input_tokens=1, total_tokens=1)}),
        "usage",
    )
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={
                "model_calls": (
                    final_infra,
                    final_infra.model_copy(
                        update={"model_call_number": 2, "provider_attempt_number": 2}
                    ),
                    exhausted_infra,
                ),
                "terminal_outcome": ExecutionOutcome.completed,
                "usage": Usage(),
                "decision_json": _decision(),
            }
        ),
        "infrastructure ledger",
    )
    with pytest.raises(ValueError, match="retry budget"):
        validate_arm_transcript(
            packet_spec,
            packet_run.model_copy(
                update={
                    "model_calls": (final_infra,),
                    "terminal_outcome": ExecutionOutcome.transport_error,
                    "usage": Usage(),
                    "decision_json": None,
                }
            ),
        )
    retried = first.model_copy(update={"model_call_number": 2, "provider_attempt_number": 2})
    validate_arm_transcript(
        packet_spec,
        packet_run.model_copy(update={"model_calls": (final_infra, retried)}),
    )
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={
                "model_calls": (
                    final_infra,
                    final_infra.model_copy(
                        update={"model_call_number": 2, "provider_attempt_number": 2}
                    ),
                    final_infra.model_copy(
                        update={"model_call_number": 3, "provider_attempt_number": 3}
                    ),
                    final_infra.model_copy(
                        update={"model_call_number": 4, "provider_attempt_number": 4}
                    ),
                ),
                "terminal_outcome": ExecutionOutcome.transport_error,
            }
        ),
        "exceeded",
    )
    rejects(
        packet_spec,
        packet_run.model_copy(
            update={
                "model_calls": (first.model_copy(update={"response": None}),),
                "terminal_outcome": ExecutionOutcome.invalid_output,
                "decision_json": None,
            }
        ),
        "missing its response",
    )
    with pytest.raises(ValueError, match="envelope"):
        _recorded_continuation(ResponsesEnvelope(raw_envelope_json=canonical_json("bad")))
    with pytest.raises(ValueError, match="output"):
        _recorded_continuation(ResponsesEnvelope(raw_envelope_json=canonical_json({"output": {}})))
    assert _recorded_continuation(
        ResponsesEnvelope(raw_envelope_json=canonical_json({"output": ["skip", {"x": 1}]}))
    ) == (canonical_json({"x": 1}),)


def test_decision_validation_handles_invalid_json_and_missing_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not _valid_decision("not-json")
    monkeypatch.setattr(
        "dsx.experiments.data_access.execution.importlib.import_module",
        lambda _: SimpleNamespace(DataAccessDecision=None),
    )
    assert _valid_decision(_decision())


def _limited(manifest: DataAccessManifest, **limits: object) -> DataAccessManifest:
    return manifest.model_copy(update={"limits": manifest.limits.model_copy(update=limits)})


def _run_one_arm(
    tmp_path: Path,
    manifest: DataAccessManifest,
    arm: str,
    script: list[ResponsesEnvelope | Exception],
    monotonic: Callable[[], float] | None = None,
) -> tuple[ArmRun, bool]:
    packet, full, combined = build_arm_specs(manifest)
    spec = {"packet": packet, "full": full, "combined": combined}[arm]
    attempt = tmp_path / "attempt"
    attempt.mkdir(parents=True)
    if monotonic is not None:
        return run_arm(
            spec,
            manifest=manifest,
            repetition_number=1,
            repetition_id="p",
            attempt_number=1,
            attempt_dir=attempt,
            client=ScriptedResponsesClient(script),
            monotonic=monotonic,
            sleep=lambda _: None,
        )
    return run_arm(
        spec,
        manifest=manifest,
        repetition_number=1,
        repetition_id="p",
        attempt_number=1,
        attempt_dir=attempt,
        client=ScriptedResponsesClient(script),
        sleep=lambda _: None,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ModelTransportError("x"), "transport_error"),
        (TimeoutError("x"), "transport_error"),
        (ConnectionError("x"), "transport_error"),
        (ModelProviderError("x"), "provider_error"),
        (type("APIConnectionError", (Exception,), {})("x"), "transport_error"),
        (type("APITimeoutError", (Exception,), {})("x"), "transport_error"),
        (type("APIStatusError", (Exception,), {})("x"), "provider_error"),
    ],
)
def test_infrastructure_classification(error: Exception, expected: str) -> None:
    assert _is_infrastructure(error) == expected
    assert _is_infrastructure(ValueError("x")) is None


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        (ResponsesEnvelope(error="bad"), "provider_error"),
        (ResponsesEnvelope(status="failed"), "provider_error"),
        (ResponsesEnvelope(status="error"), "provider_error"),
        (ResponsesEnvelope(refusal="no"), "refused"),
        (ResponsesEnvelope(status="incomplete"), "incomplete"),
        (ResponsesEnvelope(status="cancelled"), "incomplete"),
        (ResponsesEnvelope(status="canceled"), "incomplete"),
        (ResponsesEnvelope(incomplete_reason="limit"), "incomplete"),
    ],
)
def test_response_outcome_classification(envelope: ResponsesEnvelope, expected: str) -> None:
    assert _response_outcome(envelope) == expected
    assert _response_outcome(ResponsesEnvelope(status="completed")) is None


def test_contract_validators_and_append_only_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="arguments"):
        FunctionCall(call_id="c", name="f", arguments_json=canonical_json([]))
    with pytest.raises(ValidationError, match="decision JSON"):
        ResponsesEnvelope(decision_json=canonical_json([]))
    with pytest.raises(ValidationError, match="store=false"):
        ResponsesRequest(
            model_identifier="m",
            instructions="i",
            input_items_json=(canonical_json({"x": 1}),),
            max_output_tokens=1,
            response_schema_name="s",
            store=True,
        )
    request = ResponsesRequest(
        model_identifier="m",
        instructions="i",
        input_items_json=(canonical_json({"x": 1}),),
        tools_json=(canonical_json(QUERY_DATA_TOOL_SCHEMA),),
        max_output_tokens=1,
        response_schema_name="s",
    )
    assert request.input_items == ({"x": 1},)
    assert request.tools == (QUERY_DATA_TOOL_SCHEMA,)
    with pytest.raises(ValidationError, match="dsx_packet"):
        ArmExecutionSpec(
            arm=Arm.dsx_packet,
            model_identifier="m",
            system_prompt="s",
            task_prompt="t",
            max_output_tokens=1,
            packet_json=canonical_json({}),
            tools_json=(canonical_json(QUERY_DATA_TOOL_SCHEMA),),
        )
    with pytest.raises(ValidationError, match="full_data"):
        ArmExecutionSpec(
            arm=Arm.full_data,
            model_identifier="m",
            system_prompt="s",
            task_prompt="t",
            max_output_tokens=1,
            dataset_descriptor="d",
            tools_json=(canonical_json({}),),
        )
    with pytest.raises(ValidationError, match="full_data"):
        ArmExecutionSpec(
            arm=Arm.full_data,
            model_identifier="m",
            system_prompt="s",
            task_prompt="t",
            max_output_tokens=1,
            tools_json=(canonical_json(QUERY_DATA_TOOL_SCHEMA),),
        )
    with pytest.raises(ValidationError, match="each Data Access arm"):
        RepetitionAttempt(
            repetition_number=1,
            repetition_id="p",
            attempt_number=1,
            attempt_id="a",
            arm_order=(Arm.dsx_packet, Arm.dsx_packet, Arm.full_data),
            common_projection_digest="0" * 64,
            arms=(),
            terminal_status=AttemptStatus.infra_failure,
        )
    start_attempt = RepetitionAttempt(
        repetition_number=1,
        repetition_id="p",
        attempt_number=1,
        attempt_id="a",
        arm_order=(Arm.dsx_packet, Arm.full_data, Arm.packet_and_full_data),
        common_projection_digest="0" * 64,
        arms=(),
        terminal_status=AttemptStatus.infra_failure,
    )
    with pytest.raises(ValidationError, match="persisted repetition attempts"):
        RepetitionRun(
            repetition_number=1,
            repetition_id="p",
            outcome=RepetitionOutcome.infra_incomplete,
            attempts=(start_attempt, start_attempt),
        )
    valid_arm = ArmRun(
        arm=Arm.dsx_packet,
        repetition_number=1,
        repetition_id="p",
        attempt_number=1,
        common_projection_digest="0" * 64,
        terminal_outcome=ExecutionOutcome.invalid_output,
        elapsed_seconds=0,
    )
    request_for_ledger = ResponsesRequest(
        model_identifier="m",
        instructions="i",
        input_items_json=(canonical_json({"x": 1}),),
        max_output_tokens=1,
        response_schema_name="s",
    )
    with pytest.raises(ValidationError, match="requires a decision"):
        ArmRun.model_validate({**valid_arm.model_dump(), "terminal_outcome": "completed"})
    with pytest.raises(ValidationError, match="only completed"):
        ArmRun.model_validate({**valid_arm.model_dump(), "decision_json": _decision()})
    bad_model = ModelCallLedger(
        model_call_number=2,
        provider_attempt_number=1,
        request=request_for_ledger,
        elapsed_seconds=0,
        outcome=ExecutionOutcome.invalid_output,
    )
    with pytest.raises(ValidationError, match="model calls"):
        ArmRun.model_validate({**valid_arm.model_dump(), "model_calls": [bad_model]})
    bad_tool = ToolCallLedger(
        tool_call_number=2,
        model_call_number=1,
        function_call=FunctionCall(
            call_id="c", name="query_data", arguments_json=canonical_json({})
        ),
        sql_attempt=SqlAttempt(
            attempt_id="a",
            sql="",
            outcome=SqlAttemptOutcome.invalid_argument,
            elapsed_seconds=0,
            error_message="bad",
        ),
        elapsed_seconds=0,
    )
    with pytest.raises(ValidationError, match="tool calls"):
        ArmRun.model_validate({**valid_arm.model_dump(), "tool_calls": [bad_tool]})
    bad_tool = bad_tool.model_copy(update={"tool_call_number": 1})
    with pytest.raises(ValidationError, match="failed discovery"):
        ArmRun.model_validate({**valid_arm.model_dump(), "tool_calls": [bad_tool]})
    full_arm = valid_arm.model_copy(update={"arm": Arm.full_data})
    with pytest.raises(ValidationError, match="arm runs"):
        RepetitionAttempt(
            repetition_number=1,
            repetition_id="p",
            attempt_number=1,
            attempt_id="a",
            arm_order=(Arm.dsx_packet, Arm.full_data, Arm.packet_and_full_data),
            common_projection_digest="0" * 64,
            arms=(full_arm,),
            terminal_status=AttemptStatus.infra_failure,
        )
    with pytest.raises(ValidationError, match="common projection"):
        RepetitionAttempt(
            repetition_number=1,
            repetition_id="p",
            attempt_number=1,
            attempt_id="a",
            arm_order=(Arm.dsx_packet, Arm.full_data, Arm.packet_and_full_data),
            common_projection_digest="1" * 64,
            arms=(valid_arm,),
            terminal_status=AttemptStatus.infra_failure,
        )
    with pytest.raises(ValidationError, match="complete attempt"):
        RepetitionAttempt(
            repetition_number=1,
            repetition_id="p",
            attempt_number=1,
            attempt_id="a",
            arm_order=(Arm.dsx_packet, Arm.full_data, Arm.packet_and_full_data),
            common_projection_digest="0" * 64,
            arms=(valid_arm,),
            terminal_status=AttemptStatus.complete,
        )
    packet, full, combined = build_arm_specs(_manifest(tmp_path))
    with pytest.raises(ValueError, match="exactly one"):
        validate_fairness((packet,))
    changed = full.model_copy(update={"max_output_tokens": 1})
    with pytest.raises(ValueError, match="treatment"):
        validate_fairness((packet, changed, combined))
    with pytest.raises(FileExistsError):
        _write(tmp_path / "once.json", Usage())
        _write(tmp_path / "once.json", Usage())
    assert _initial_input(packet) != _initial_input(full)
    assert _request(packet, _initial_input(packet)).tools == ()


def test_usage_and_openai_normalization_and_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dumpable:
        def model_dump(self) -> dict[str, object]:
            return {
                "input_tokens": 8,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens": 4,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 12,
            }

    assert _usage_from_value(None) == Usage()
    assert _usage_from_value(Dumpable()) == Usage(
        input_tokens=8,
        cached_input_tokens=3,
        output_tokens=4,
        reasoning_tokens=2,
        total_tokens=12,
    )
    assert _usage_from_value({"input_tokens": -1, "total_tokens": "bad"}) == Usage()

    class Parsed:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return cast(dict[str, object], json.loads(_decision()))

    class FakeResponse:
        output_parsed = Parsed()

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "id": "r",
                "status": "completed",
                "service_tier": "priority",
                "usage": {"input_tokens": 1, "total_tokens": 1},
                "error": {"code": "x"},
                "incomplete_details": {"reason": "stop"},
                "output": [
                    "not-a-map",
                    {
                        "type": "reasoning",
                        "id": "reasoning-id",
                        "encrypted_content": "encrypted",
                        "content": [],
                        "status": None,
                    },
                    {
                        "type": "function_call",
                        "id": "id",
                        "name": "query_data",
                        "arguments": "{bad",
                        "caller": None,
                        "namespace": None,
                        "status": "completed",
                    },
                    {
                        "type": "function_call",
                        "call_id": "c2",
                        "name": "query_data",
                        "arguments": {"sql": "SELECT 1"},
                    },
                    {
                        "type": "message",
                        "content": [
                            "skip",
                            {"type": "other"},
                            {"type": "output_text", "text": "not-json"},
                            {"type": "output_text", "text": _decision()},
                            {"type": "output_text", "text": '"scalar"'},
                            {"refusal": "no"},
                        ],
                    },
                ],
            }

    normalized = _normalize_openai_response(FakeResponse())
    assert normalized.decision_json == _decision()
    assert normalized.refusal == "no" and normalized.incomplete_reason == "stop"
    assert len(normalized.function_calls) == 2
    assert normalized.function_calls[0].arguments == {"_invalid_arguments": "{bad"}
    continuation = tuple(json.loads(item) for item in normalized.continuation_items_json)
    assert continuation[0] == {
        "type": "reasoning",
        "id": "reasoning-id",
        "encrypted_content": "encrypted",
        "content": [],
    }
    assert continuation[1]["status"] == "completed"
    assert "caller" not in continuation[1] and "namespace" not in continuation[1]
    assert '"status":null' in normalized.raw_envelope_json
    assert _recorded_continuation(normalized) == normalized.continuation_items_json
    assert _normalize_openai_response("scalar").raw_envelope_json == canonical_json(
        {"response": "scalar"}
    )
    assert _normalize_openai_response(SimpleNamespace(output_parsed=[])).decision_json is None

    received: list[dict[str, object]] = []

    class FakeCreate:
        def create(self, **kwargs: object) -> FakeResponse:
            received.append(kwargs)
            return FakeResponse()

    client = OpenAIResponsesClient(SimpleNamespace(responses=FakeCreate()))
    request = ResponsesRequest(
        model_identifier="m",
        instructions="i",
        input_items_json=(canonical_json({"role": "user"}),),
        tools_json=(canonical_json(QUERY_DATA_TOOL_SCHEMA),),
        reasoning_effort="low",
        service_tier="priority",
        max_output_tokens=10,
        response_schema_name="decision",
    )
    assert client.create(request).response_id == "r"
    assert received[0]["store"] is False and received[0]["parallel_tool_calls"] is False
    assert "tools" in received[0] and "reasoning" in received[0] and "service_tier" in received[0]
    plain = ResponsesRequest(
        model_identifier="m",
        instructions="i",
        input_items_json=(canonical_json({"role": "user"}),),
        max_output_tokens=10,
        response_schema_name="decision",
    )
    client.create(plain)
    assert "tools" not in received[1] and "reasoning" not in received[1]
    assert "service_tier" not in received[1]
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **_: SimpleNamespace(responses=FakeCreate()))
    assert OpenAIResponsesClient().create(request).status == "completed"


def test_arm_terminal_paths_and_repetition_collision(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    wall = _limited(manifest, arm_wall_clock_seconds=1)
    clock = iter((0.0, 1.0, 1.0)).__next__
    arm, infra = _run_one_arm(tmp_path / "wall", wall, "packet", [], monotonic=clock)
    assert arm.terminal_outcome == "wall_clock_limit" and not infra

    function = FunctionCall(
        call_id="c", name="query_data", arguments_json=canonical_json({"sql": "SELECT 1"})
    )
    call = _reply(call=function)
    limit = _limited(manifest, model_calls_per_arm=1)
    arm, _ = _run_one_arm(tmp_path / "model", limit, "full", [call])
    assert arm.terminal_outcome == "model_call_limit"
    combined, _ = _run_one_arm(
        tmp_path / "combined-tool", manifest, "combined", [call, _reply(decision=_decision())]
    )
    assert combined.terminal_outcome is ExecutionOutcome.completed
    assert len(combined.tool_calls) == 1
    packet_call, _ = _run_one_arm(tmp_path / "packet-call", manifest, "packet", [call])
    assert packet_call.terminal_outcome == "invalid_output"

    two_calls = ResponsesEnvelope(
        status="completed",
        raw_envelope_json=canonical_json({}),
        function_calls=(
            function,
            FunctionCall(call_id="d", name="other", arguments_json=canonical_json({})),
        ),
        continuation_items_json=(),
    )
    tool_limit = _limited(manifest, sql_attempts_per_arm=1)
    arm, _ = _run_one_arm(tmp_path / "tool", tool_limit, "full", [two_calls])
    assert arm.terminal_outcome == "tool_call_limit"
    unknown, _ = _run_one_arm(
        tmp_path / "unknown", manifest, "full", [two_calls, _reply(decision=_decision())]
    )
    assert unknown.failed_discovery_attempts >= 1
    result_item = parse_canonical_json(
        _function_result_item(function, unknown.tool_calls[0].sql_attempt)
    )
    assert isinstance(result_item["output"], str)
    assert parse_canonical_json(result_item["output"]) == unknown.tool_calls[
        0
    ].sql_attempt.model_dump(mode="json")
    invalid, _ = _run_one_arm(
        tmp_path / "invalid", manifest, "packet", [ResponsesEnvelope(status="completed")]
    )
    assert invalid.terminal_outcome == "invalid_output"
    refusal, _ = _run_one_arm(
        tmp_path / "refusal", manifest, "packet", [ResponsesEnvelope(refusal="no")]
    )
    assert refusal.terminal_outcome == "refused"
    incomplete, _ = _run_one_arm(
        tmp_path / "incomplete", manifest, "packet", [ResponsesEnvelope(status="incomplete")]
    )
    assert incomplete.terminal_outcome == "incomplete"

    with pytest.raises(RuntimeError):
        _run_one_arm(tmp_path / "raise", manifest, "packet", [RuntimeError("boom")])
    repetition = run_repetition(
        manifest,
        run_root=tmp_path / "pairs",
        repetition_number=1,
        repetition_id="a",
        client=ScriptedResponsesClient([_reply(decision=_decision())] * 3),
        order_seed=1,
        attempt_id_factory=iter(("a",)).__next__,
    )
    assert repetition.outcome == "complete"
    with pytest.raises(FileExistsError):
        run_repetition(
            manifest,
            run_root=tmp_path / "pairs",
            repetition_number=1,
            repetition_id="a",
            client=ScriptedResponsesClient([]),
            order_seed=1,
            attempt_id_factory=iter(("b",)).__next__,
        )


@pytest.mark.parametrize(
    "low_budget_result",
    (
        ModelTransportError("transport"),
        ModelProviderError("provider"),
        ResponsesEnvelope(status="failed"),
    ),
)
def test_provider_retries_infra_completion_and_experiment_repetitions(
    tmp_path: Path, low_budget_result: ResponsesEnvelope | Exception
) -> None:
    manifest = _manifest(tmp_path)
    provider_retry, infra = _run_one_arm(
        tmp_path / "provider",
        manifest,
        "packet",
        [ResponsesEnvelope(status="failed"), _reply(decision=_decision())],
    )
    assert provider_retry.terminal_outcome == "completed" and not infra
    exhausted, infra = _run_one_arm(
        tmp_path / "provider-exhausted",
        manifest,
        "packet",
        [ResponsesEnvelope(status="failed")] * 3,
    )
    assert exhausted.terminal_outcome == "provider_error" and infra
    limited, infra = _run_one_arm(
        tmp_path / "retry-limit",
        _limited(manifest, model_calls_per_arm=1),
        "packet",
        [low_budget_result],
    )
    assert limited.terminal_outcome == "model_call_limit" and not infra
    packet, _full_data, _combined = build_arm_specs(manifest)
    validate_arm_transcript(packet, limited, model_calls_per_arm=1)
    with pytest.raises(ValueError, match="infrastructure ledger"):
        validate_arm_transcript(
            packet,
            limited.model_copy(update={"terminal_outcome": ExecutionOutcome.transport_error}),
            model_calls_per_arm=1,
        )
    failed = run_repetition(
        manifest,
        run_root=tmp_path / "failed",
        repetition_number=1,
        repetition_id="a",
        client=ScriptedResponsesClient([ModelTransportError("x")] * 6),
        order_seed=1,
        attempt_id_factory=iter(("a", "b")).__next__,
        sleep=lambda _: None,
    )
    assert failed.outcome == "infra_incomplete" and len(failed.attempts) == 2
    repeated = run_experiment(
        _limited(manifest, repetitions=2),
        run_root=tmp_path / "experiment",
        client=ScriptedResponsesClient([_reply(decision=_decision())] * 6),
        order_seed=7,
        repetition_id_factory=lambda number: f"x{number}",
        attempt_id_factory=iter(("a", "b")).__next__,
    )
    assert tuple(item.repetition_id for item in repeated) == ("x1", "x2")
    default_repeated = run_experiment(
        _limited(manifest, repetitions=1),
        run_root=tmp_path / "default-experiment",
        client=ScriptedResponsesClient([_reply(decision=_decision())] * 3),
        order_seed=8,
    )
    assert default_repeated[0].repetition_id == "repetition-001"
    assert _valid_decision(_decision()) and not _valid_decision(canonical_json([]))


def test_repetition_contracts_require_causal_retries_and_coherent_outcomes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    completed = run_repetition(
        manifest,
        run_root=tmp_path / "complete",
        repetition_number=1,
        repetition_id="r",
        client=ScriptedResponsesClient([_reply(decision=_decision())] * 3),
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )
    attempt = completed.attempts[0]
    bad_complete = attempt.model_dump()
    bad_complete["arms"][0]["terminal_outcome"] = ExecutionOutcome.transport_error
    bad_complete["arms"][0]["decision_json"] = None
    with pytest.raises(ValidationError, match="complete attempt cannot"):
        RepetitionAttempt.model_validate(bad_complete)
    with pytest.raises(ValidationError, match="exhausted infrastructure"):
        RepetitionAttempt.model_validate(
            {**attempt.model_dump(), "terminal_status": AttemptStatus.infra_failure}
        )
    with pytest.raises(ValidationError, match="only infrastructure failures"):
        RepetitionRun.model_validate({**completed.model_dump(), "attempts": (attempt, attempt)})
    with pytest.raises(ValidationError, match="incomplete repetition"):
        RepetitionRun.model_validate(
            {**completed.model_dump(), "outcome": RepetitionOutcome.infra_incomplete}
        )
    failed = run_repetition(
        manifest,
        run_root=tmp_path / "failed",
        repetition_number=1,
        repetition_id="failed",
        client=ScriptedResponsesClient([ModelTransportError("offline")] * 6),
        order_seed=1,
        attempt_id_factory=iter(("attempt-1", "attempt-2")).__next__,
        sleep=lambda _: None,
    )
    with pytest.raises(ValidationError, match="complete repetition"):
        RepetitionRun.model_validate({**failed.model_dump(), "outcome": RepetitionOutcome.complete})
    with pytest.raises(ValidationError, match="two exhausted"):
        RepetitionRun.model_validate({**failed.model_dump(), "attempts": (failed.attempts[-1],)})


def test_transcript_terminal_outcome_reconciliation_guards(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    packet, full_data, _combined = build_arm_specs(manifest)
    wall, _ = _run_one_arm(
        tmp_path / "wall",
        _limited(manifest, arm_wall_clock_seconds=1),
        "packet",
        [],
        monotonic=iter((0.0, 1.0, 1.0)).__next__,
    )
    validate_arm_transcript(packet, wall)
    completed, _ = _run_one_arm(
        tmp_path / "completed", manifest, "packet", [_reply(decision=_decision())]
    )
    with pytest.raises(ValueError, match="decision conflicts"):
        validate_arm_transcript(
            packet,
            completed.model_copy(
                update={"terminal_outcome": ExecutionOutcome.invalid_output, "decision_json": None}
            ),
        )
    empty = completed.model_copy(
        update={
            "model_calls": (),
            "usage": Usage(),
            "decision_json": None,
            "terminal_outcome": ExecutionOutcome.invalid_output,
        }
    )
    with pytest.raises(ValueError, match="no model-call basis"):
        validate_arm_transcript(packet, empty)
    missing_response = completed.model_calls[0].model_copy(update={"response": None})
    with pytest.raises(ValueError, match="missing its response"):
        validate_arm_transcript(
            packet,
            completed.model_copy(
                update={
                    "model_calls": (missing_response,),
                    "usage": Usage(),
                    "decision_json": None,
                    "terminal_outcome": ExecutionOutcome.invalid_output,
                }
            ),
        )
    refused, _ = _run_one_arm(
        tmp_path / "refused", manifest, "packet", [ResponsesEnvelope(refusal="no")]
    )
    with pytest.raises(ValueError, match="refusal conflicts"):
        validate_arm_transcript(
            packet, refused.model_copy(update={"terminal_outcome": ExecutionOutcome.invalid_output})
        )
    incomplete, _ = _run_one_arm(
        tmp_path / "incomplete", manifest, "packet", [ResponsesEnvelope(status="incomplete")]
    )
    validate_arm_transcript(packet, incomplete)
    with pytest.raises(ValueError, match="incomplete status"):
        validate_arm_transcript(
            packet,
            incomplete.model_copy(update={"terminal_outcome": ExecutionOutcome.invalid_output}),
        )
    function = FunctionCall(
        call_id="query", name="query_data", arguments_json=canonical_json({"sql": "SELECT 1"})
    )
    tool_limited, _ = _run_one_arm(
        tmp_path / "tools",
        _limited(manifest, model_calls_per_arm=1),
        "full",
        [_reply(call=function)],
    )
    validate_arm_transcript(full_data, tool_limited)
    with pytest.raises(ValueError, match="tool-response"):
        validate_arm_transcript(
            full_data,
            tool_limited.model_copy(update={"terminal_outcome": ExecutionOutcome.invalid_output}),
        )
    invalid, _ = _run_one_arm(
        tmp_path / "invalid", manifest, "packet", [ResponsesEnvelope(status="completed")]
    )
    validate_arm_transcript(packet, invalid)
    with pytest.raises(ValueError, match="invalid model"):
        validate_arm_transcript(
            packet, invalid.model_copy(update={"terminal_outcome": ExecutionOutcome.refused})
        )
