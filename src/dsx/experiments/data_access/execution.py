"""Three-arm, append-only execution for the Data Access experiment.

The runner deliberately has a small provider boundary.  It is usable with the
OpenAI Responses API, but its :class:`ScriptedResponsesClient` means every
state-machine and accounting test can run without credentials or network.
"""

from __future__ import annotations

import importlib
import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import Field, model_validator

from .canonical import canonical_digest, canonical_json, parse_canonical_json
from .models import Arm, DataAccessContract, DataAccessManifest, SqlAttempt
from .sql_tool import QUERY_DATA_TOOL_SCHEMA, ReadOnlySqlTool

EVIDENCE_PROTOCOL_VERSION = "v1"

_PACKET_EVIDENCE_PROTOCOL = (
    "Evidence protocol v1: cite packet facts with evidence kind `packet_json_pointer` and an "
    "RFC 6901 pointer into dsx_packet. A packet pointer supports a factual claim only when the "
    "resolved value is exactly the claim's asserted_value; a nearby object, parent, or related "
    "fact is not support."
)

_FULL_DATA_EVIDENCE_PROTOCOL = (
    "Evidence protocol v1: cite each factual claim with evidence kind `tool_call` and the "
    "successful query_data evidence_id. Each cited aggregate must prove one claim and use these "
    "exact aliases/result shapes: row_count -> one row (row_count); class_count or class_rate -> "
    "one row (label, class_count) or (label, class_rate), filtered by the target column; "
    "majority_baseline -> one row (majority_baseline) using count and max; review_count -> one "
    "row (review_count) using count and the claim fraction; missingness -> one row "
    "(column, missingness), using count and IS NULL; uniqueness -> one row (column, uniqueness), "
    "using count(distinct); likely_id -> one row (column, likely_id), using count(distinct); "
    "recommended_exclusions -> one or more one-column rows named excluded_column, using "
    "count(distinct). For class predicates include WHERE <target column>; aggregate against "
    "dataset and do not mix unrelated facts in a cited result."
)

_PACKET_AND_FULL_DATA_EVIDENCE_PROTOCOL = (
    "Evidence protocol v1: factual claims may cite either the packet evidence protocol "
    "(`packet_json_pointer` with an RFC 6901 pointer into dsx_packet) or the data evidence "
    "protocol (`tool_call` with a successful query_data evidence_id). Follow the applicable "
    "protocol exactly for each claim."
)


class ExecutionOutcome(StrEnum):
    completed = "completed"
    refused = "refused"
    incomplete = "incomplete"
    invalid_output = "invalid_output"
    model_call_limit = "model_call_limit"
    tool_call_limit = "tool_call_limit"
    wall_clock_limit = "wall_clock_limit"
    transport_error = "transport_error"
    provider_error = "provider_error"


class RepetitionOutcome(StrEnum):
    complete = "complete"
    infra_incomplete = "infra_incomplete"


class AttemptStatus(StrEnum):
    complete = "complete"
    infra_failure = "infra_failure"


class Usage(DataAccessContract):
    """Provider-neutral token counters, including Responses reasoning usage."""

    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def plus(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class FunctionCall(DataAccessContract):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_arguments(self) -> FunctionCall:
        value = parse_canonical_json(self.arguments_json)
        if not isinstance(value, dict):
            raise ValueError("function arguments must be a JSON object")
        return self

    @property
    def arguments(self) -> dict[str, Any]:
        value = parse_canonical_json(self.arguments_json)
        assert isinstance(value, dict)  # protected by valid_arguments
        return value


class ResponsesEnvelope(DataAccessContract):
    """The normalized portion of one non-streaming Responses API response."""

    response_id: str | None = None
    status: str | None = None
    raw_envelope_json: str = Field(default="{}")
    usage: Usage = Field(default_factory=Usage)
    service_tier: str | None = None
    function_calls: tuple[FunctionCall, ...] = ()
    decision_json: str | None = None
    refusal: str | None = None
    incomplete_reason: str | None = None
    error: str | None = None
    continuation_items_json: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_json_fields(self) -> ResponsesEnvelope:
        parse_canonical_json(self.raw_envelope_json)
        if self.decision_json is not None:
            value = parse_canonical_json(self.decision_json)
            if not isinstance(value, dict):
                raise ValueError("decision JSON must be an object")
        for item in self.continuation_items_json:
            parse_canonical_json(item)
        return self


class ResponsesRequest(DataAccessContract):
    """One exact request at the provider boundary; persisted before calling it."""

    model_identifier: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    input_items_json: tuple[str, ...] = Field(min_length=1)
    tools_json: tuple[str, ...] = ()
    reasoning_effort: str | None = None
    service_tier: str | None = None
    max_output_tokens: int = Field(gt=0)
    response_schema_name: str = Field(min_length=1)
    store: bool = False
    parallel_tool_calls: bool = False

    @model_validator(mode="after")
    def validate_request(self) -> ResponsesRequest:
        if self.store or self.parallel_tool_calls:
            raise ValueError("Data Access requests require store=false and sequential tools")
        for item in (*self.input_items_json, *self.tools_json):
            parse_canonical_json(item)
        return self

    @property
    def input_items(self) -> tuple[Any, ...]:
        return tuple(parse_canonical_json(item) for item in self.input_items_json)

    @property
    def tools(self) -> tuple[Any, ...]:
        return tuple(parse_canonical_json(item) for item in self.tools_json)


class ModelCallLedger(DataAccessContract):
    model_call_number: int = Field(gt=0)
    provider_attempt_number: int = Field(gt=0)
    request: ResponsesRequest
    elapsed_seconds: float = Field(ge=0.0)
    outcome: ExecutionOutcome
    response: ResponsesEnvelope | None = None
    diagnostic: str | None = None


class ModelRequestStart(DataAccessContract):
    """Crash-safe proof of the exact provider request emitted before the call starts."""

    model_call_number: int = Field(gt=0)
    provider_attempt_number: int = Field(gt=0)
    request: ResponsesRequest


class ToolCallLedger(DataAccessContract):
    tool_call_number: int = Field(gt=0)
    model_call_number: int = Field(gt=0)
    function_call: FunctionCall
    sql_attempt: SqlAttempt
    elapsed_seconds: float = Field(ge=0.0)


class ArmExecutionSpec(DataAccessContract):
    """Rendered arm.  Packet bytes are opaque; only its committed digest is compared."""

    arm: Arm
    model_identifier: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    task_prompt: str = Field(min_length=1)
    reasoning_effort: str | None = None
    service_tier: str | None = None
    max_output_tokens: int = Field(gt=0)
    response_schema_name: str = "data_access_decision"
    evidence_protocol_version: str = EVIDENCE_PROTOCOL_VERSION
    packet_json: str | None = None
    dataset_descriptor: str | None = None
    tools_json: tuple[str, ...] = ()

    @property
    def common_projection(self) -> dict[str, Any]:
        return {
            "model_identifier": self.model_identifier,
            "system_prompt": self.system_prompt,
            "task_prompt": self.task_prompt,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "max_output_tokens": self.max_output_tokens,
            "response_schema_name": self.response_schema_name,
            "evidence_protocol_version": self.evidence_protocol_version,
        }

    @property
    def common_projection_digest(self) -> str:
        return canonical_digest(self.common_projection)

    @model_validator(mode="after")
    def treatment_is_narrow(self) -> ArmExecutionSpec:
        if self.arm is Arm.dsx_packet:
            if self.packet_json is None or self.dataset_descriptor is not None or self.tools_json:
                raise ValueError("dsx_packet requires only packet context and no tools")
            parse_canonical_json(self.packet_json)
        elif self.arm is Arm.full_data:
            if (
                self.packet_json is not None
                or not self.dataset_descriptor
                or len(self.tools_json) != 1
            ):
                raise ValueError("full_data requires only a dataset descriptor and query tool")
            if parse_canonical_json(self.tools_json[0]) != QUERY_DATA_TOOL_SCHEMA:
                raise ValueError("full_data must expose exactly query_data")
        else:
            if (
                self.packet_json is None
                or not self.dataset_descriptor
                or len(self.tools_json) != 1
            ):
                raise ValueError(
                    "packet_and_full_data requires packet context, a dataset descriptor, "
                    "and query tool"
                )
            parse_canonical_json(self.packet_json)
            if parse_canonical_json(self.tools_json[0]) != QUERY_DATA_TOOL_SCHEMA:
                raise ValueError("packet_and_full_data must expose exactly query_data")
        return self


class ArmRun(DataAccessContract):
    arm: Arm
    repetition_number: int = Field(gt=0)
    repetition_id: str = Field(min_length=1)
    attempt_number: int = Field(gt=0)
    common_projection_digest: str = Field(min_length=64, max_length=64)
    terminal_outcome: ExecutionOutcome
    elapsed_seconds: float = Field(ge=0.0)
    usage: Usage = Field(default_factory=Usage)
    model_calls: tuple[ModelCallLedger, ...] = ()
    tool_calls: tuple[ToolCallLedger, ...] = ()
    decision_json: str | None = None
    failed_discovery_attempts: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def arm_run_is_consistent(self) -> ArmRun:
        if self.terminal_outcome is ExecutionOutcome.completed and self.decision_json is None:
            raise ValueError("completed arm requires a decision")
        if (
            self.terminal_outcome is not ExecutionOutcome.completed
            and self.decision_json is not None
        ):
            raise ValueError("only completed arm may retain a decision")
        if tuple(call.model_call_number for call in self.model_calls) != tuple(
            range(1, len(self.model_calls) + 1)
        ):
            raise ValueError("model calls must be contiguous")
        if tuple(call.tool_call_number for call in self.tool_calls) != tuple(
            range(1, len(self.tool_calls) + 1)
        ):
            raise ValueError("tool calls must be contiguous")
        failures = sum(call.sql_attempt.outcome != "success" for call in self.tool_calls)
        if self.failed_discovery_attempts != failures:
            raise ValueError("failed discovery attempts must match SQL ledger")
        return self


class RepetitionAttempt(DataAccessContract):
    repetition_number: int = Field(gt=0)
    repetition_id: str = Field(min_length=1)
    attempt_number: int = Field(gt=0)
    attempt_id: str = Field(min_length=1)
    arm_order: tuple[Arm, Arm, Arm]
    common_projection_digest: str = Field(min_length=64, max_length=64)
    arms: tuple[ArmRun, ...]
    terminal_status: AttemptStatus

    @model_validator(mode="after")
    def repetition_attempt_is_consistent(self) -> RepetitionAttempt:
        if set(self.arm_order) != set(Arm):
            raise ValueError("arm order must contain each Data Access arm once")
        if tuple(run.arm for run in self.arms) != self.arm_order[: len(self.arms)]:
            raise ValueError("arm runs must follow the recorded arm order")
        if any(run.common_projection_digest != self.common_projection_digest for run in self.arms):
            raise ValueError("arm common projection must match repetition attempt")
        if self.terminal_status is AttemptStatus.complete:
            if len(self.arms) != len(Arm):
                raise ValueError("complete attempt requires every arm")
            if any(
                run.terminal_outcome
                in {ExecutionOutcome.transport_error, ExecutionOutcome.provider_error}
                for run in self.arms
            ):
                raise ValueError("complete attempt cannot contain an infrastructure failure")
        else:
            # ``attempt_start.json`` commits an empty, not-yet-executed attempt.
            # Its completed companion is reconciled by the run-root validator.
            if not self.arms:
                return self
            final = self.arms[-1]
            if (
                final.terminal_outcome
                not in {ExecutionOutcome.transport_error, ExecutionOutcome.provider_error}
                or not final.model_calls
                or final.model_calls[-1].outcome is not final.terminal_outcome
                or final.model_calls[-1].provider_attempt_number != 3
            ):
                raise ValueError(
                    "infrastructure failure requires a final exhausted infrastructure error"
                )
        return self


class RepetitionRun(DataAccessContract):
    repetition_number: int = Field(gt=0)
    repetition_id: str = Field(min_length=1)
    outcome: RepetitionOutcome
    attempts: tuple[RepetitionAttempt, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def repetition_run_is_consistent(self) -> RepetitionRun:
        if any(
            attempt.terminal_status is AttemptStatus.infra_failure and not attempt.arms
            for attempt in self.attempts
        ):
            raise ValueError(
                "persisted repetition attempts require exhausted infrastructure evidence"
            )
        if any(
            attempt.terminal_status is not AttemptStatus.infra_failure
            for attempt in self.attempts[:-1]
        ):
            raise ValueError("only infrastructure failures may be retried")
        final = self.attempts[-1]
        if self.outcome is RepetitionOutcome.complete:
            if final.terminal_status is not AttemptStatus.complete:
                raise ValueError("complete repetition requires a complete final attempt")
        elif (
            len(self.attempts) != 2
            or final.terminal_status is not AttemptStatus.infra_failure
        ):
            raise ValueError("incomplete repetition requires two exhausted infrastructure attempts")
        return self


class ResponsesClient(Protocol):
    """Small injectable boundary for stateless Responses execution."""

    def create(self, request: ResponsesRequest) -> ResponsesEnvelope:
        """Return one normalized provider response."""


class ScriptedResponsesClient:
    """Offline, deterministic Responses boundary for execution tests."""

    def __init__(self, script: Sequence[ResponsesEnvelope | Exception]) -> None:
        self._script = iter(script)
        self.requests: list[ResponsesRequest] = []

    def create(self, request: ResponsesRequest) -> ResponsesEnvelope:
        self.requests.append(request)
        result = next(self._script)
        if isinstance(result, Exception):
            raise result
        return result


class ModelTransportError(Exception):
    """Retryable connection failure exposed by a Responses client."""


class ModelProviderError(Exception):
    """Retryable provider failure exposed by a Responses client."""


class OpenAIResponsesClient:
    """A minimal production Responses adapter; no state is stored server-side."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from openai import OpenAI

            client = OpenAI(max_retries=0)
        self._client = client

    def create(self, request: ResponsesRequest) -> ResponsesEnvelope:
        kwargs: dict[str, Any] = {
            "model": request.model_identifier,
            "instructions": request.instructions,
            "input": list(request.input_items),
            "store": False,
            "parallel_tool_calls": False,
            "max_output_tokens": request.max_output_tokens,
            "text": {"format": _decision_response_format(request.response_schema_name)},
        }
        if request.tools:
            kwargs["tools"] = list(request.tools)
        if request.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": request.reasoning_effort}
        if request.service_tier is not None:
            kwargs["service_tier"] = request.service_tier
        response = self._client.responses.create(**kwargs)
        return _normalize_openai_response(response)


def _decision_response_format(name: str) -> dict[str, Any]:
    """Use evaluation's one shared Responses-compatible terminal schema."""
    module = importlib.import_module("dsx.experiments.data_access.evaluation")
    return cast(dict[str, Any], module.provider_decision_schema(name))


def _json_string(value: Any) -> str:
    return canonical_json(value)


def _usage_from_value(value: Any) -> Usage:
    if value is None:
        return Usage()
    if not isinstance(value, Mapping):
        value = getattr(value, "model_dump", lambda: {})()
    details = value.get("input_tokens_details", {}) if isinstance(value, Mapping) else {}
    output_details = value.get("output_tokens_details", {}) if isinstance(value, Mapping) else {}

    def number(key: str, source: Any = value) -> int:
        raw = source.get(key, 0) if isinstance(source, Mapping) else 0
        return int(raw) if isinstance(raw, int | float) and raw >= 0 else 0

    return Usage(
        input_tokens=number("input_tokens"),
        cached_input_tokens=number("cached_tokens", details),
        output_tokens=number("output_tokens"),
        reasoning_tokens=number("reasoning_tokens", output_details),
        total_tokens=number("total_tokens"),
    )


def _response_item_as_input(value: Any) -> Any:
    """Remove SDK-only null fields before replaying a response item as input.

    The SDK's response models serialize optional output fields such as ``status``,
    ``caller``, and ``namespace`` as explicit nulls.  Those fields are not valid null
    values in the Responses input union, so stateless continuation requests must omit
    them while retaining every populated response field.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _response_item_as_input(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list | tuple):
        return [_response_item_as_input(item) for item in value]
    return value


def _normalize_openai_response(response: Any) -> ResponsesEnvelope:
    dumped = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
    if not isinstance(dumped, Mapping):
        dumped = {"response": str(dumped)}
    calls: list[FunctionCall] = []
    continuation: list[str] = []
    decision: dict[str, Any] | None = None
    refusal: str | None = None
    for output in dumped.get("output", ()):
        if not isinstance(output, Mapping):
            continue
        item = dict(output)
        continuation.append(_json_string(_response_item_as_input(item)))
        if item.get("type") == "function_call":
            arguments = item.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments_json = _json_string(json.loads(arguments))
                except json.JSONDecodeError:
                    arguments_json = _json_string({"_invalid_arguments": arguments})
            else:
                arguments_json = _json_string(arguments)
            calls.append(
                FunctionCall(
                    call_id=str(item.get("call_id") or item.get("id") or "missing-call-id"),
                    name=str(item.get("name") or ""),
                    arguments_json=arguments_json,
                )
            )
        if item.get("type") == "message":
            for part in item.get("content", ()):
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    try:
                        candidate = json.loads(str(part["text"]))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict):
                        decision = candidate
                if isinstance(part.get("refusal"), str):
                    refusal = str(part["refusal"])
    parsed = getattr(response, "output_parsed", None)
    if parsed is not None:
        if hasattr(parsed, "model_dump"):
            parsed = parsed.model_dump(mode="json")
        if isinstance(parsed, Mapping):
            decision = dict(parsed)
    incomplete = dumped.get("incomplete_details")
    reason = incomplete.get("reason") if isinstance(incomplete, Mapping) else incomplete
    return ResponsesEnvelope(
        response_id=str(dumped["id"]) if dumped.get("id") is not None else None,
        status=str(dumped["status"]) if dumped.get("status") is not None else None,
        raw_envelope_json=_json_string(dict(dumped)),
        usage=_usage_from_value(dumped.get("usage")),
        service_tier=str(dumped["service_tier"]) if dumped.get("service_tier") else None,
        function_calls=tuple(calls),
        decision_json=_json_string(decision) if decision is not None else None,
        refusal=refusal,
        incomplete_reason=str(reason) if reason else None,
        error=_json_string(dumped["error"]) if dumped.get("error") is not None else None,
        continuation_items_json=tuple(continuation),
    )


def _dataset_descriptor() -> str:
    return (
        "You have full read-only access to every row and column in the DuckDB "
        "table named dataset. Use query_data(sql) to investigate it."
    )


def build_arm_specs(
    manifest: DataAccessManifest,
) -> tuple[ArmExecutionSpec, ArmExecutionSpec, ArmExecutionSpec]:
    """Render the only permitted treatment difference from a frozen manifest."""
    model = manifest.model
    return (
        ArmExecutionSpec(
            arm=Arm.dsx_packet,
            model_identifier=model.model_identifier,
            system_prompt=model.system_prompt,
            task_prompt=manifest.case.task_prompt,
            reasoning_effort=model.reasoning_effort,
            service_tier=model.service_tier,
            max_output_tokens=model.max_output_tokens,
            packet_json=manifest.packet.canonical_json,
        ),
        ArmExecutionSpec(
            arm=Arm.full_data,
            model_identifier=model.model_identifier,
            system_prompt=model.system_prompt,
            task_prompt=manifest.case.task_prompt,
            reasoning_effort=model.reasoning_effort,
            service_tier=model.service_tier,
            max_output_tokens=model.max_output_tokens,
            dataset_descriptor=_dataset_descriptor(),
            tools_json=(canonical_json(QUERY_DATA_TOOL_SCHEMA),),
        ),
        ArmExecutionSpec(
            arm=Arm.packet_and_full_data,
            model_identifier=model.model_identifier,
            system_prompt=model.system_prompt,
            task_prompt=manifest.case.task_prompt,
            reasoning_effort=model.reasoning_effort,
            service_tier=model.service_tier,
            max_output_tokens=model.max_output_tokens,
            packet_json=manifest.packet.canonical_json,
            dataset_descriptor=_dataset_descriptor(),
            tools_json=(canonical_json(QUERY_DATA_TOOL_SCHEMA),),
        ),
    )


def validate_fairness(specs: Sequence[ArmExecutionSpec]) -> str:
    """Fail closed unless all arms share every common model/task setting."""
    if len(specs) != len(Arm) or {spec.arm for spec in specs} != set(Arm):
        raise ValueError("exactly one specification is required for each Data Access arm")
    digests = {spec.common_projection_digest for spec in specs}
    if len(digests) != 1:
        raise ValueError("arms differ outside the permitted treatment boundary")
    # Revalidate arm-local treatment so unchecked construction cannot weaken it.
    for spec in specs:
        ArmExecutionSpec.model_validate(spec.model_dump(mode="python"))
    return digests.pop()


def _initial_input(spec: ArmExecutionSpec) -> tuple[str, ...]:
    context: dict[str, Any]
    if spec.arm is Arm.dsx_packet:
        context = {
            "dsx_packet": parse_canonical_json(spec.packet_json or "{}"),
            "evidence_protocol_version": spec.evidence_protocol_version,
            "evidence_protocol": _PACKET_EVIDENCE_PROTOCOL,
        }
    elif spec.arm is Arm.full_data:
        context = {
            "dataset": spec.dataset_descriptor,
            "evidence_protocol_version": spec.evidence_protocol_version,
            "evidence_protocol": _FULL_DATA_EVIDENCE_PROTOCOL,
        }
    else:
        context = {
            "dsx_packet": parse_canonical_json(spec.packet_json or "{}"),
            "dataset": spec.dataset_descriptor,
            "evidence_protocol_version": spec.evidence_protocol_version,
            "evidence_protocol": _PACKET_AND_FULL_DATA_EVIDENCE_PROTOCOL,
        }
    return (
        canonical_json(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": canonical_json(
                            {"task_prompt": spec.task_prompt, "context": context}
                        ),
                    }
                ],
            }
        ),
    )


def _request(spec: ArmExecutionSpec, input_items: tuple[str, ...]) -> ResponsesRequest:
    return ResponsesRequest(
        model_identifier=spec.model_identifier,
        instructions=spec.system_prompt,
        input_items_json=input_items,
        tools_json=spec.tools_json,
        reasoning_effort=spec.reasoning_effort,
        service_tier=spec.service_tier,
        max_output_tokens=spec.max_output_tokens,
        response_schema_name=spec.response_schema_name,
    )


def _is_infrastructure(error: Exception) -> ExecutionOutcome | None:
    if isinstance(error, (ModelTransportError, TimeoutError, ConnectionError)):
        return ExecutionOutcome.transport_error
    if isinstance(error, ModelProviderError):
        return ExecutionOutcome.provider_error
    # The OpenAI exception imports stay optional for lean offline environments.
    name = type(error).__name__
    if name in {"APIConnectionError", "APITimeoutError"}:
        return ExecutionOutcome.transport_error
    if name == "APIStatusError":
        return ExecutionOutcome.provider_error
    return None


def _response_outcome(response: ResponsesEnvelope) -> ExecutionOutcome | None:
    if response.error is not None or response.status in {"failed", "error"}:
        return ExecutionOutcome.provider_error
    if response.refusal is not None:
        return ExecutionOutcome.refused
    if response.status in {"incomplete", "cancelled", "canceled"} or response.incomplete_reason:
        return ExecutionOutcome.incomplete
    return None


def _valid_decision(decision_json: str) -> bool:
    """Use the evaluator's schema when available without creating an import cycle."""
    try:
        candidate = parse_canonical_json(decision_json)
        if not isinstance(candidate, dict):
            return False
        module = importlib.import_module("dsx.experiments.data_access.evaluation")
        decision_type = getattr(module, "DataAccessDecision", None)
        if decision_type is None:
            return True
        decision_type.model_validate(candidate)
        return True
    except Exception:
        return False


def _write(path: Path, contract: DataAccessContract) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _arm_dir(attempt_dir: Path, arm: Arm) -> Path:
    return attempt_dir / "arms" / arm.value


def _write_model_call(arm_dir: Path, ledger: ModelCallLedger) -> None:
    filename = f"{ledger.model_call_number:03d}-{ledger.provider_attempt_number}.json"
    _write(arm_dir / "model_calls" / filename, ledger)


def _write_model_request_start(arm_dir: Path, start: ModelRequestStart) -> None:
    filename = f"{start.model_call_number:03d}-{start.provider_attempt_number}.json"
    _write(arm_dir / "model_requests" / filename, start)


def _write_tool_call(arm_dir: Path, ledger: ToolCallLedger) -> None:
    _write(arm_dir / "tool_calls" / f"{ledger.tool_call_number:03d}.json", ledger)


def _function_result_item(call: FunctionCall, attempt: SqlAttempt) -> str:
    payload: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": call.call_id,
        "output": canonical_json(attempt.model_dump(mode="json")),
    }
    return canonical_json(payload)


def _recorded_continuation(response: ResponsesEnvelope) -> tuple[str, ...]:
    """Recover the exact continuation items that a normalized Responses reply promised."""
    envelope = parse_canonical_json(response.raw_envelope_json)
    if not isinstance(envelope, Mapping):
        raise ValueError("response envelope must be an object")
    output = envelope.get("output", ())
    if not isinstance(output, list | tuple):
        raise ValueError("response output must be an array")
    return tuple(
        canonical_json(_response_item_as_input(dict(item)))
        for item in output
        if isinstance(item, Mapping)
    )


def validate_arm_transcript(
    spec: ArmExecutionSpec,
    arm_run: ArmRun,
    *,
    model_calls_per_arm: int | None = None,
) -> None:
    """Fail closed unless a persisted arm ledger replays from its exact treatment spec.

    This validates the model-visible transcript only.  Callers validating a run root
    should additionally compare its on-disk ``model_requests`` artifacts to the
    corresponding model-call request objects before treating the transcript as complete.
    """
    ArmExecutionSpec.model_validate(spec.model_dump(mode="python"))
    if model_calls_per_arm is not None and model_calls_per_arm <= 0:
        raise ValueError("model_calls_per_arm must be positive when validating a transcript")
    if arm_run.arm is not spec.arm:
        raise ValueError("arm transcript crosses treatment arms")
    if arm_run.common_projection_digest != spec.common_projection_digest:
        raise ValueError("arm transcript common projection does not match its specification")
    if spec.arm is Arm.dsx_packet and arm_run.tool_calls:
        raise ValueError("packet arm transcript contains extra tool call (data tool)")
    input_items = _initial_input(spec)
    tool_index = 0
    expected_provider_attempt = 1
    model_calls = arm_run.model_calls
    if model_calls_per_arm is not None and len(model_calls) > model_calls_per_arm:
        raise ValueError("model call ledger exceeds the configured arm limit")
    for index, model_call in enumerate(model_calls, start=1):
        if model_call.model_call_number != index:
            raise ValueError("model call numbering is not contiguous")
        if model_call.provider_attempt_number != expected_provider_attempt:
            raise ValueError("provider retry numbering does not match transcript")
        expected_request = _request(spec, input_items)
        if model_call.request != expected_request:
            raise ValueError("persisted model request does not match reconstructed transcript")
        if model_call.outcome in {
            ExecutionOutcome.transport_error,
            ExecutionOutcome.provider_error,
        }:
            if index == len(model_calls):
                continue
            if expected_provider_attempt >= 3:
                raise ValueError("infrastructure retry exceeded the three-attempt budget")
            expected_provider_attempt += 1
            continue
        if model_call.response is None:
            raise ValueError("non-infrastructure model outcome is missing its response")
        response = model_call.response
        if response.function_calls:
            if spec.arm not in {Arm.full_data, Arm.packet_and_full_data}:
                raise ValueError("packet arm transcript contains a data tool call")
            if model_call.outcome is not ExecutionOutcome.completed:
                raise ValueError("tool response has a non-completed provider outcome")
            continuation = _recorded_continuation(response)
            if continuation != response.continuation_items_json:
                raise ValueError("response continuation does not match the recorded raw output")
            next_input = list(input_items)
            next_input.extend(continuation)
            for function_call in response.function_calls:
                if tool_index >= len(arm_run.tool_calls):
                    raise ValueError("tool response is missing its persisted tool call")
                tool_call = arm_run.tool_calls[tool_index]
                if tool_call.tool_call_number != tool_index + 1:
                    raise ValueError("tool call numbering is not contiguous")
                if tool_call.model_call_number != model_call.model_call_number:
                    raise ValueError("tool call is associated with the wrong model response")
                if tool_call.function_call != function_call:
                    raise ValueError("tool calls do not match response function calls in order")
                next_input.append(_function_result_item(function_call, tool_call.sql_attempt))
                tool_index += 1
            input_items = tuple(next_input)
            expected_provider_attempt = 1
            continue
        if index != len(model_calls):
            raise ValueError("terminal model response is followed by an extra request")
    if tool_index != len(arm_run.tool_calls):
        raise ValueError("persisted transcript contains an extra tool call")
    usage = Usage()
    for model_call in arm_run.model_calls:
        if model_call.response is not None:
            usage = usage.plus(model_call.response.usage)
    if arm_run.usage != usage:
        raise ValueError("arm usage does not match model response ledger")
    if not model_calls:
        if arm_run.terminal_outcome is not ExecutionOutcome.wall_clock_limit:
            raise ValueError("arm terminal outcome has no model-call basis")
        return
    final = model_calls[-1]
    if final.outcome in {ExecutionOutcome.transport_error, ExecutionOutcome.provider_error}:
        retry_blocked_by_call_limit = (
            model_calls_per_arm is not None
            and len(model_calls) == model_calls_per_arm
            and final.provider_attempt_number < 3
        )
        expected_terminal = (
            ExecutionOutcome.model_call_limit if retry_blocked_by_call_limit else final.outcome
        )
        if arm_run.terminal_outcome is not expected_terminal or arm_run.decision_json is not None:
            raise ValueError("arm terminal outcome conflicts with infrastructure ledger")
        if not retry_blocked_by_call_limit and final.provider_attempt_number != 3:
            raise ValueError("infrastructure ledger ended before retry budget was exhausted")
        return
    # The replay loop above rejects a response-less non-infrastructure call.
    response = cast(ResponsesEnvelope, final.response)
    if response.decision_json is not None and _valid_decision(response.decision_json):
        if (
            arm_run.terminal_outcome is not ExecutionOutcome.completed
            or arm_run.decision_json != response.decision_json
        ):
            raise ValueError("arm terminal decision conflicts with model response")
    elif response.refusal is not None:
        if arm_run.terminal_outcome is not ExecutionOutcome.refused:
            raise ValueError("arm terminal refusal conflicts with model response")
    elif response.incomplete_reason is not None or response.status in {
        "incomplete",
        "cancelled",
        "canceled",
    }:
        if arm_run.terminal_outcome is not ExecutionOutcome.incomplete:
            raise ValueError("arm terminal incomplete status conflicts with model response")
    elif response.function_calls:
        if arm_run.terminal_outcome not in {
            ExecutionOutcome.tool_call_limit,
            ExecutionOutcome.model_call_limit,
            ExecutionOutcome.wall_clock_limit,
        }:
            raise ValueError("arm terminal outcome conflicts with tool-response ledger")
    elif arm_run.terminal_outcome is not ExecutionOutcome.invalid_output:
        raise ValueError("arm terminal outcome conflicts with invalid model response")


def run_arm(
    spec: ArmExecutionSpec,
    *,
    manifest: DataAccessManifest,
    repetition_number: int,
    repetition_id: str,
    attempt_number: int,
    attempt_dir: Path,
    client: ResponsesClient,
    monotonic: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ArmRun, bool]:
    """Run one fresh arm.  ``True`` means exhausted infrastructure retry budget."""
    arm_dir = _arm_dir(attempt_dir, spec.arm)
    arm_dir.mkdir(parents=True, exist_ok=False)
    _write(arm_dir / "arm_spec.json", spec)
    started = monotonic()
    usage = Usage()
    model_calls: list[ModelCallLedger] = []
    tool_calls: list[ToolCallLedger] = []
    input_items = _initial_input(spec)
    tool = (
        ReadOnlySqlTool(
            manifest.dataset.database_path,
            timeout_seconds=manifest.limits.sql_timeout_seconds,
            max_rows=manifest.limits.sql_max_rows,
            max_result_bytes=manifest.limits.sql_max_result_bytes,
        )
        if spec.arm in {Arm.full_data, Arm.packet_and_full_data}
        else None
    )
    terminal = ExecutionOutcome.model_call_limit
    decision: str | None = None
    infrastructure_exhausted = False
    model_number = 0
    while len(model_calls) < manifest.limits.model_calls_per_arm:
        if monotonic() - started >= manifest.limits.arm_wall_clock_seconds:
            terminal = ExecutionOutcome.wall_clock_limit
            break
        request = _request(spec, input_items)
        response: ResponsesEnvelope | None = None
        provider_attempt = 1
        while True:
            if len(model_calls) >= manifest.limits.model_calls_per_arm:
                terminal = ExecutionOutcome.model_call_limit
                break
            model_number = len(model_calls) + 1
            _write_model_request_start(
                arm_dir,
                ModelRequestStart(
                    model_call_number=model_number,
                    provider_attempt_number=provider_attempt,
                    request=request,
                ),
            )
            call_started = monotonic()
            try:
                response = client.create(request)
            except Exception as error:
                outcome = _is_infrastructure(error)
                if outcome is None:
                    raise
                ledger = ModelCallLedger(
                    model_call_number=model_number,
                    provider_attempt_number=provider_attempt,
                    request=request,
                    elapsed_seconds=monotonic() - call_started,
                    outcome=outcome,
                    diagnostic=f"{type(error).__name__}: {error}",
                )
                model_calls.append(ledger)
                _write_model_call(arm_dir, ledger)
                if provider_attempt < 3:
                    sleep(float(2 ** (provider_attempt - 1)))
                    provider_attempt += 1
                    continue
                terminal = outcome
                infrastructure_exhausted = True
                break
            else:
                classification = _response_outcome(response)
                ledger = ModelCallLedger(
                    model_call_number=model_number,
                    provider_attempt_number=provider_attempt,
                    request=request,
                    elapsed_seconds=monotonic() - call_started,
                    outcome=classification or ExecutionOutcome.completed,
                    response=response,
                    diagnostic=response.error or response.refusal or response.incomplete_reason,
                )
                model_calls.append(ledger)
                _write_model_call(arm_dir, ledger)
                usage = usage.plus(response.usage)
                if classification is not None:
                    terminal = classification
                    if classification is ExecutionOutcome.provider_error and provider_attempt < 3:
                        # A retry may be blocked by the arm call cap.  Do not let this
                        # failed provider envelope fall through as an invalid response.
                        response = None
                        sleep(float(2 ** (provider_attempt - 1)))
                        provider_attempt += 1
                        continue
                    if classification is ExecutionOutcome.provider_error:
                        infrastructure_exhausted = True
                    response = None
                break
        if infrastructure_exhausted or response is None:
            break
        if response.function_calls:
            if tool is None:
                terminal = ExecutionOutcome.invalid_output
                break
            continuation = list(input_items)
            continuation.extend(response.continuation_items_json)
            for call in response.function_calls:
                if len(tool_calls) >= manifest.limits.sql_attempts_per_arm:
                    terminal = ExecutionOutcome.tool_call_limit
                    break
                tool_started = monotonic()
                if call.name != "query_data":
                    sql_attempt = tool.execute(None, ordinal=len(tool_calls) + 1)
                else:
                    sql_attempt = tool.execute(
                        call.arguments.get("sql"), ordinal=len(tool_calls) + 1
                    )
                event = ToolCallLedger(
                    tool_call_number=len(tool_calls) + 1,
                    model_call_number=model_number,
                    function_call=call,
                    sql_attempt=sql_attempt,
                    elapsed_seconds=monotonic() - tool_started,
                )
                tool_calls.append(event)
                _write_tool_call(arm_dir, event)
                continuation.append(_function_result_item(call, sql_attempt))
            else:
                input_items = tuple(continuation)
                continue
            break
        if response.decision_json is not None and _valid_decision(response.decision_json):
            terminal = ExecutionOutcome.completed
            decision = response.decision_json
        else:
            terminal = ExecutionOutcome.invalid_output
        break
    arm_run = ArmRun(
        arm=spec.arm,
        repetition_number=repetition_number,
        repetition_id=repetition_id,
        attempt_number=attempt_number,
        common_projection_digest=spec.common_projection_digest,
        terminal_outcome=terminal,
        elapsed_seconds=monotonic() - started,
        usage=usage,
        model_calls=tuple(model_calls),
        tool_calls=tuple(tool_calls),
        decision_json=decision,
        failed_discovery_attempts=sum(
            event.sql_attempt.outcome != "success" for event in tool_calls
        ),
    )
    _write(arm_dir / "arm_run.json", arm_run)
    return arm_run, infrastructure_exhausted


def run_repetition(
    manifest: DataAccessManifest,
    *,
    run_root: Path,
    repetition_number: int,
    repetition_id: str,
    client: ResponsesClient,
    order_seed: int,
    attempt_id_factory: Callable[[], str],
    specs: Sequence[ArmExecutionSpec] | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> RepetitionRun:
    """Execute one repetition, restarting every arm once after infrastructure exhaustion."""
    rendered = tuple(specs or build_arm_specs(manifest))
    common_digest = validate_fairness(rendered)
    by_arm = {spec.arm: spec for spec in rendered}
    repetition_dir = run_root / f"repetition-{repetition_number}-{repetition_id}"
    repetition_dir.mkdir(parents=True, exist_ok=False)
    attempts: list[RepetitionAttempt] = []
    rng = random.Random(order_seed)
    for attempt_number in (1, 2):
        selected = rng.sample(list(Arm), k=len(Arm))
        arm_order: tuple[Arm, Arm, Arm] = (selected[0], selected[1], selected[2])
        attempt_id = attempt_id_factory()
        attempt_dir = repetition_dir / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True)
        start = RepetitionAttempt(
            repetition_number=repetition_number,
            repetition_id=repetition_id,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            arm_order=arm_order,
            common_projection_digest=common_digest,
            arms=(),
            terminal_status=AttemptStatus.infra_failure,
        )
        _write(attempt_dir / "attempt_start.json", start)
        arm_runs: list[ArmRun] = []
        infra = False
        for arm in arm_order:
            arm_run, infra = run_arm(
                by_arm[arm],
                manifest=manifest,
                repetition_number=repetition_number,
                repetition_id=repetition_id,
                attempt_number=attempt_number,
                attempt_dir=attempt_dir,
                client=client,
                monotonic=monotonic,
                sleep=sleep,
            )
            arm_runs.append(arm_run)
            if infra:
                break
        summary = RepetitionAttempt(
            repetition_number=repetition_number,
            repetition_id=repetition_id,
            attempt_number=attempt_number,
            attempt_id=attempt_id,
            arm_order=arm_order,
            common_projection_digest=common_digest,
            arms=tuple(arm_runs),
            terminal_status=AttemptStatus.infra_failure if infra else AttemptStatus.complete,
        )
        _write(attempt_dir / "repetition_attempt.json", summary)
        attempts.append(summary)
        if not infra:
            repetition = RepetitionRun(
                repetition_number=repetition_number,
                repetition_id=repetition_id,
                outcome=RepetitionOutcome.complete,
                attempts=tuple(attempts),
            )
            _write(repetition_dir / "repetition_run.json", repetition)
            return repetition
    repetition = RepetitionRun(
        repetition_number=repetition_number,
        repetition_id=repetition_id,
        outcome=RepetitionOutcome.infra_incomplete,
        attempts=tuple(attempts),
    )
    _write(repetition_dir / "repetition_run.json", repetition)
    return repetition


def run_experiment(
    manifest: DataAccessManifest,
    *,
    run_root: Path,
    client: ResponsesClient,
    order_seed: int,
    repetition_id_factory: Callable[[int], str] | None = None,
    attempt_id_factory: Callable[[], str] | None = None,
) -> tuple[RepetitionRun, ...]:
    """Run the manifest's seeded three-arm repetitions with fresh state per arm."""
    rng = random.Random(order_seed)
    make_repetition_id = repetition_id_factory or (lambda number: f"repetition-{number:03d}")
    counter = 0

    def next_attempt() -> str:
        nonlocal counter
        counter += 1
        return f"attempt-{counter:04d}"

    make_attempt = attempt_id_factory or next_attempt
    return tuple(
        run_repetition(
            manifest,
            run_root=run_root,
            repetition_number=number,
            repetition_id=make_repetition_id(number),
            client=client,
            order_seed=rng.randrange(2**63),
            attempt_id_factory=make_attempt,
        )
        for number in range(1, manifest.limits.repetitions + 1)
    )
