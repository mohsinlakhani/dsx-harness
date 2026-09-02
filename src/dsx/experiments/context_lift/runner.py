"""Sequential Context Lift execution with append-only attempt artifacts."""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ValidationError, create_model

from .models import (
    AnalysisDecision,
    Arm,
    ArmOutcome,
    AttemptStart,
    AttemptSummary,
    AttemptTerminalStatus,
    ModelRequest,
    OutcomeKind,
    PairOutcomeKind,
    PairSummary,
    PilotContract,
)
from .render import RenderedArm, RenderedRequests, canonicalize_request


class ModelTransportError(Exception):
    """A retryable connection or timeout failure at the client boundary."""


class ModelProviderError(Exception):
    """A retryable provider-side status failure at the client boundary."""


class ModelReply(PilotContract):
    """Provider-neutral reply information used for centralized classification."""

    parsed_decision: AnalysisDecision | None = None
    raw_response: str | None = None
    status: str | None = None
    error: str | None = None
    refusal: str | None = None
    incomplete_reason: str | None = None


class ModelClient(Protocol):
    """The only client boundary used by the runner."""

    def complete(self, request: ModelRequest) -> ModelReply:
        """Return the typed response for one exact rendered request."""


class ScriptedModelClient:
    """Deterministic offline client that records requests and replays a script."""

    def __init__(self, script: Sequence[ModelReply | Exception]) -> None:
        self._script = iter(script)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        reply = next(self._script)
        if isinstance(reply, Exception):
            raise reply
        return reply


class OpenAIModelClient:
    """Minimal non-streaming OpenAI Responses adapter."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from openai import OpenAI

            client = OpenAI(max_retries=0)
        self._client = client

    def complete(self, request: ModelRequest) -> ModelReply:
        response_format = create_model(
            request.response_schema_name,
            __base__=AnalysisDecision,
        )
        raw = self._client.responses.with_raw_response.parse(
            model=request.model_identifier,
            instructions=request.system_prompt,
            input=[
                {
                    "role": "user",
                    "content": canonicalize_request(
                        {"context": request.context, "task_prompt": request.task_prompt}
                    ),
                }
            ],
            text_format=response_format,
            store=False,
        )
        try:
            response = raw.parse()
        except ValidationError:
            return _reply_from_raw_envelope(raw)
        raw_response = _raw_response_json(raw)
        parsed = _parsed_decision(getattr(response, "output_parsed", None))
        return ModelReply(
            parsed_decision=parsed,
            raw_response=raw_response,
            status=_string_value(getattr(response, "status", None)),
            error=_string_value(getattr(response, "error", None)),
            refusal=_response_refusal(response),
            incomplete_reason=_string_value(getattr(response, "incomplete_details", None)),
        )


def _raw_response_json(raw: Any) -> str:
    return str(raw.http_response.text)


def _reply_from_raw_envelope(raw: Any) -> ModelReply:
    payload = raw.http_response.json()
    if not isinstance(payload, dict):
        return ModelReply(raw_response=_raw_response_json(raw))
    incomplete_details = payload.get("incomplete_details")
    incomplete_reason = (
        _string_value(incomplete_details.get("reason"))
        if isinstance(incomplete_details, dict)
        else _string_value(incomplete_details)
    )
    return ModelReply(
        raw_response=_raw_response_json(raw),
        status=_string_value(payload.get("status")),
        error=_string_value(payload.get("error")),
        refusal=_payload_refusal(payload),
        incomplete_reason=incomplete_reason,
    )


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return str(value)


def _parsed_decision(value: Any) -> AnalysisDecision | None:
    if value is None:
        return None
    if isinstance(value, AnalysisDecision):
        return AnalysisDecision.model_validate(value.model_dump(mode="python"))
    try:
        return AnalysisDecision.model_validate(value)
    except Exception:
        return None


def _response_refusal(response: Any) -> str | None:
    direct = _string_value(getattr(response, "refusal", None))
    if direct:
        return direct
    for output in getattr(response, "output", ()):
        for content in getattr(output, "content", ()):
            refusal = _string_value(getattr(content, "refusal", None))
            if refusal:
                return refusal
    return None


def _payload_refusal(payload: dict[str, Any]) -> str | None:
    direct = _string_value(payload.get("refusal"))
    if direct:
        return direct
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                refusal = _string_value(part.get("refusal"))
                if refusal:
                    return refusal
    return None


def _exception_kind(error: Exception) -> OutcomeKind | None:
    if isinstance(error, (ModelTransportError, APITimeoutError, APIConnectionError)):
        return OutcomeKind.transport_error
    if isinstance(error, (TimeoutError, ConnectionError)):
        return OutcomeKind.transport_error
    if isinstance(error, (ModelProviderError, APIStatusError)):
        return OutcomeKind.provider_error
    return None


def _exception_raw_response(error: Exception) -> str | None:
    if isinstance(error, APIStatusError):
        if error.response.text:
            return error.response.text
        body = error.body
        if isinstance(body, str):
            return body
        if body is not None:
            return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
    return None


def _reply_kind(reply: ModelReply) -> OutcomeKind:
    if reply.error is not None or reply.status in {"failed", "error"}:
        return OutcomeKind.provider_error
    if reply.refusal is not None:
        return OutcomeKind.refused
    if (
        reply.status in {"incomplete", "cancelled", "canceled"}
        or reply.incomplete_reason is not None
    ):
        return OutcomeKind.incomplete
    if reply.parsed_decision is None:
        return OutcomeKind.invalid_output
    return OutcomeKind.completed


def _write_contract(path: Path, contract: PilotContract) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _write_outcome(attempt_dir: Path, outcome: ArmOutcome) -> None:
    outcome_dir = attempt_dir / "outcomes"
    outcome_dir.mkdir(exist_ok=True)
    _write_contract(outcome_dir / f"{outcome.request_number:02d}-{outcome.arm.value}.json", outcome)


def _pair_dir(run_root: Path, pair_number: int, pair_id: str) -> Path:
    return run_root / f"pair-{pair_number}-{pair_id}"


def _create_attempt(
    *,
    run_root: Path,
    pair_number: int,
    pair_id: str,
    attempt_number: int,
    attempt_id: str,
    rendered: RenderedRequests,
    arm_order: tuple[Arm, Arm],
) -> tuple[Path, AttemptStart]:
    pair_dir = _pair_dir(run_root, pair_number, pair_id)
    pair_dir.mkdir(parents=True, exist_ok=True)
    if (pair_dir / "pair_summary.json").exists():
        raise FileExistsError(pair_dir / "pair_summary.json")
    attempts_dir = pair_dir / "attempts"
    attempts_dir.mkdir(exist_ok=True)
    attempt_dir = attempts_dir / attempt_id
    attempt_dir.mkdir()
    start = AttemptStart(
        pair_number=pair_number,
        pair_id=pair_id,
        attempt_number=attempt_number,
        attempt_id=attempt_id,
        arm_order=arm_order,
        common_projection_digest=rendered.common_projection_digest,
    )
    _write_contract(attempt_dir / "rendered_requests.json", rendered)
    _write_contract(attempt_dir / "attempt_start.json", start)
    return attempt_dir, start


def _outcome(
    arm: RenderedArm,
    kind: OutcomeKind,
    attempt_number: int,
    request_number: int,
    reply: ModelReply | None = None,
    error: Exception | None = None,
) -> ArmOutcome:
    diagnostic = None
    raw_response = None
    decision = None
    if reply is not None:
        raw_response = reply.raw_response
        decision = reply.parsed_decision if kind is OutcomeKind.completed else None
        diagnostic = reply.error or reply.refusal or reply.incomplete_reason
        if diagnostic is None and kind is not OutcomeKind.completed:
            diagnostic = f"response status: {reply.status or 'unclassified'}"
    if error is not None:
        diagnostic = f"{type(error).__name__}: {error}"
        raw_response = _exception_raw_response(error)
    return ArmOutcome(
        arm=arm.label,
        outcome_kind=kind,
        request_digest=arm.request_digest,
        common_projection_digest=arm.common_projection_digest,
        attempt_number=attempt_number,
        request_number=request_number,
        parsed_decision=decision,
        raw_response=raw_response,
        diagnostic=diagnostic,
    )


def _run_arm(
    arm: RenderedArm,
    *,
    attempt_number: int,
    attempt_dir: Path,
    client: ModelClient,
    sleep: Callable[[float], None],
) -> tuple[list[ArmOutcome], bool]:
    outcomes: list[ArmOutcome] = []
    for request_number in range(1, 4):
        try:
            reply = client.complete(arm.request)
        except Exception as error:
            kind = _exception_kind(error)
            if kind is None:
                raise
            outcome = _outcome(arm, kind, attempt_number, request_number, error=error)
        else:
            kind = _reply_kind(reply)
            outcome = _outcome(arm, kind, attempt_number, request_number, reply=reply)
        outcomes.append(outcome)
        _write_outcome(attempt_dir, outcome)
        if kind not in {OutcomeKind.transport_error, OutcomeKind.provider_error}:
            return outcomes, False
        if request_number < 3:
            sleep(float(2 ** (request_number - 1)))
    return outcomes, True


def run_pair(
    rendered: RenderedRequests,
    *,
    run_root: Path,
    pair_number: int,
    pair_id: str,
    client: ModelClient,
    order_seed: int,
    attempt_id_factory: Callable[[], str],
    sleep: Callable[[float], None] = time.sleep,
) -> PairSummary:
    """Run at most two fresh sequential pair attempts and publish their ledger."""
    rng = random.Random(order_seed)
    summaries: list[AttemptSummary] = []

    # new pair -> attempt 1 -> complete
    #                     \-> infra failure -> attempt 2 -> complete
    #                                                    \-> infra_incomplete
    for attempt_number in (1, 2):
        ordered_arms = tuple(rng.sample(rendered.arms, k=2))
        arm_order = (ordered_arms[0].label, ordered_arms[1].label)
        attempt_dir, start = _create_attempt(
            run_root=run_root,
            pair_number=pair_number,
            pair_id=pair_id,
            attempt_number=attempt_number,
            attempt_id=attempt_id_factory(),
            rendered=rendered,
            arm_order=arm_order,
        )
        outcomes: list[ArmOutcome] = []
        infrastructure_exhausted = False
        for arm in ordered_arms:
            arm_outcomes, infrastructure_exhausted = _run_arm(
                arm,
                attempt_number=attempt_number,
                attempt_dir=attempt_dir,
                client=client,
                sleep=sleep,
            )
            outcomes.extend(arm_outcomes)
            if infrastructure_exhausted:
                break
        status = (
            AttemptTerminalStatus.infra_failure
            if infrastructure_exhausted
            else AttemptTerminalStatus.complete
        )
        summary = AttemptSummary(start=start, outcomes=tuple(outcomes), terminal_status=status)
        _write_contract(attempt_dir / "attempt_summary.json", summary)
        summaries.append(summary)
        if not infrastructure_exhausted:
            pair = PairSummary(
                pair_number=pair_number,
                pair_id=pair_id,
                outcome_kind=PairOutcomeKind.complete,
                attempts=tuple(summaries),
            )
            _write_contract(_pair_dir(run_root, pair_number, pair_id) / "pair_summary.json", pair)
            return pair

    pair = PairSummary(
        pair_number=pair_number,
        pair_id=pair_id,
        outcome_kind=PairOutcomeKind.infra_incomplete,
        attempts=tuple(summaries),
    )
    _write_contract(_pair_dir(run_root, pair_number, pair_id) / "pair_summary.json", pair)
    return pair
