"""Tests for the durable, sequential paired-model runner."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from openai import (
    APIStatusError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    UnprocessableEntityError,
)

from dsx.pilot.models import AnalysisDecision, Arm, ArmOutcome, Metric, OutcomeKind
from dsx.pilot.render import RequestConfiguration, render_requests
from dsx.pilot.runner import ModelReply


def _decision() -> AnalysisDecision:
    return AnalysisDecision(
        primary_metric=Metric.recall_at_5_percent,
        supporting_metrics=(Metric.precision_at_5_percent,),
        review_budget_fraction=0.05,
        split_strategy="stratified validation",
        excluded_columns=("row_id",),
        reasoning="Ranking is constrained to five percent.",
        limitations=("Synthetic case",),
        recommendation="Use a ranked classifier.",
        packet_citations=(),
    )


def _rendered():  # type: ignore[no-untyped-def]
    from dsx.pilot.models import candidate_packet, generate_pilot_case

    return render_requests(
        generate_pilot_case(seed=7),
        candidate_packet(),
        RequestConfiguration(
            model_identifier="gpt-test",
            system_prompt="Return structured analysis.",
            response_schema_name="analysis_decision",
        ),
    )


def _responses_payload(status: str, text: str) -> dict[str, object]:
    return {
        "id": "resp-test",
        "object": "response",
        "created_at": 1,
        "model": "gpt-4o",
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [
            {
                "id": "msg-test",
                "type": "message",
                "role": "assistant",
                "status": "incomplete" if status == "incomplete" else "completed",
                "content": [
                    {"type": "output_text", "text": text, "annotations": []},
                ],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "status": status,
    }


def _sdk_client(*payloads: dict[str, object]) -> OpenAI:
    responses = iter(payloads)

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=next(responses), request=request)

    return OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        max_retries=0,
    )


def test_runner_calls_arms_sequentially_in_recorded_seeded_order(tmp_path: Path) -> None:
    """Changing execution to parallel or ignoring the frozen order seed breaks the ledger."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    rendered = _rendered()
    client = ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 2)

    pair = run_pair(
        rendered,
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )

    assert tuple(request.context.profile_packet is not None for request in client.requests) == (
        tuple(arm is Arm.packet_on for arm in pair.attempts[0].start.arm_order)
    )
    assert pair.attempts[0].start.arm_order == (Arm.packet_off, Arm.packet_on)
    assert pair.outcome_kind == "complete"


def test_infrastructure_retries_reuse_the_exact_request_and_backoff(tmp_path: Path) -> None:
    """Replacing a retry request or changing the 1, 2 delays invalidates comparability."""
    from dsx.pilot.runner import (
        ModelReply,
        ModelTransportError,
        ScriptedModelClient,
        run_pair,
    )

    rendered = _rendered()
    client = ScriptedModelClient(
        [
            ModelTransportError("lost"),
            ModelTransportError("lost"),
            ModelReply(parsed_decision=_decision()),
            ModelReply(parsed_decision=_decision()),
        ]
    )
    delays: list[float] = []

    pair = run_pair(
        rendered,
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        sleep=delays.append,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )

    assert client.requests[0] is client.requests[1] is client.requests[2]
    assert delays == [1, 2]
    assert tuple(outcome.request_number for outcome in pair.attempts[0].outcomes[:3]) == (1, 2, 3)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("transport", OutcomeKind.transport_error),
        ("provider", OutcomeKind.provider_error),
        ("refusal", OutcomeKind.refused),
        ("incomplete", OutcomeKind.incomplete),
        ("invalid", OutcomeKind.invalid_output),
        ("completed", OutcomeKind.completed),
    ],
)
def test_runner_classifies_every_reply_kind(
    tmp_path: Path, reply: str, expected: OutcomeKind
) -> None:
    """Removing a classification branch makes a durable outcome ambiguous."""
    from dsx.pilot.runner import (
        ModelProviderError,
        ModelReply,
        ModelTransportError,
        ScriptedModelClient,
        run_pair,
    )

    first = {
        "transport": ModelTransportError("offline"),
        "provider": ModelProviderError("bad gateway"),
        "refusal": ModelReply(refusal="cannot help"),
        "incomplete": ModelReply(status="incomplete", incomplete_reason="max_output_tokens"),
        "invalid": ModelReply(raw_response="{}"),
        "completed": ModelReply(parsed_decision=_decision(), raw_response='{"ok":true}'),
    }[reply]
    # Infrastructure replies exhaust only after all three calls; terminal client scripts
    # keep the other arm available for a successful rerun when necessary.
    script = [first]
    if reply in {"transport", "provider"}:
        script.extend(
            [
                first,
                first,
                ModelReply(parsed_decision=_decision()),
                ModelReply(parsed_decision=_decision()),
            ]
        )
    else:
        script.append(ModelReply(parsed_decision=_decision()))
    client = ScriptedModelClient(script)

    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    assert pair.attempts[0].outcomes[0].outcome_kind is expected


def test_refusal_is_terminal_for_its_arm_and_pair(tmp_path: Path) -> None:
    """Retrying refusals would turn a model behavior into infrastructure noise."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    client = ScriptedModelClient(
        [ModelReply(refusal="No."), ModelReply(parsed_decision=_decision())]
    )
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )

    assert len(client.requests) == 2
    assert pair.outcome_kind == "complete"
    assert pair.attempts[0].outcomes[0].outcome_kind is OutcomeKind.refused


def test_first_exhausted_infrastructure_attempt_reruns_both_arms(tmp_path: Path) -> None:
    """Reusing a first-attempt success would violate fresh paired execution."""
    from dsx.pilot.runner import ModelReply, ModelTransportError, ScriptedModelClient, run_pair

    client = ScriptedModelClient(
        [
            ModelTransportError("offline"),
            ModelTransportError("offline"),
            ModelTransportError("offline"),
            ModelReply(parsed_decision=_decision()),
            ModelReply(parsed_decision=_decision()),
        ]
    )
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    assert pair.outcome_kind == "complete"
    assert len(pair.attempts) == 2
    assert len(client.requests) == 5
    assert pair.attempts[0].terminal_status == "infra_failure"


def test_second_exhausted_infrastructure_attempt_is_terminal(tmp_path: Path) -> None:
    """A third pair attempt would exceed the experimental retry contract."""
    from dsx.pilot.runner import ModelTransportError, ScriptedModelClient, run_pair

    client = ScriptedModelClient([ModelTransportError("offline")] * 6)
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    assert pair.outcome_kind == "infra_incomplete"
    assert len(pair.attempts) == 2
    assert len(client.requests) == 6


def test_artifacts_are_written_before_calls_and_round_trip(tmp_path: Path) -> None:
    """Writing artifacts after calling the provider would lose crash-recovery evidence."""
    from dsx.pilot.models import AttemptStart, AttemptSummary, PairSummary
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    class InspectingClient(ScriptedModelClient):
        def complete(self, request):  # type: ignore[no-untyped-def]
            attempt = tmp_path / "pair-4-pair-a" / "attempts" / "attempt-a"
            assert (attempt / "attempt_start.json").is_file()
            assert (attempt / "rendered_requests.json").is_file()
            return super().complete(request)

    client = InspectingClient([ModelReply(parsed_decision=_decision())] * 2)
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )
    attempt_dir = tmp_path / "pair-4-pair-a" / "attempts" / "attempt-a"

    assert AttemptStart.model_validate_json((attempt_dir / "attempt_start.json").read_text())
    assert AttemptSummary.model_validate_json((attempt_dir / "attempt_summary.json").read_text())
    assert (
        PairSummary.model_validate_json(
            (tmp_path / "pair-4-pair-a" / "pair_summary.json").read_text()
        )
        == pair
    )
    assert json.loads((attempt_dir / "rendered_requests.json").read_text())["packet_off"][
        "request_digest"
    ]


def test_unexpected_exception_leaves_started_attempt_and_collision_never_overwrites(
    tmp_path: Path,
) -> None:
    """A crash must preserve evidence, and a reused attempt ID must fail safely."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    crashed = ScriptedModelClient([RuntimeError("unexpected")])
    with pytest.raises(RuntimeError, match="unexpected"):
        run_pair(
            _rendered(),
            run_root=tmp_path,
            pair_number=4,
            pair_id="pair-a",
            client=crashed,
            order_seed=1,
            attempt_id_factory=iter(("attempt-a",)).__next__,
        )
    attempt = tmp_path / "pair-4-pair-a" / "attempts" / "attempt-a"
    assert (attempt / "attempt_start.json").is_file()
    assert not (attempt / "attempt_summary.json").exists()

    with pytest.raises(FileExistsError):
        run_pair(
            _rendered(),
            run_root=tmp_path,
            pair_number=4,
            pair_id="pair-a",
            client=ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 2),
            order_seed=1,
            attempt_id_factory=iter(("attempt-a",)).__next__,
        )


def test_classification_precedence_prefers_provider_failure_over_all_response_fields(
    tmp_path: Path,
) -> None:
    """Moving refusal or incompleteness ahead of provider failure masks infrastructure errors."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    client = ScriptedModelClient(
        [
            ModelReply(
                parsed_decision=_decision(),
                status="failed",
                error="server error",
                refusal="no",
                incomplete_reason="budget",
            )
        ]
        * 6
    )
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    assert pair.attempts[0].outcomes[0].outcome_kind is OutcomeKind.provider_error
    assert pair.outcome_kind == "infra_incomplete"


def test_fresh_id_after_crash_runs_without_resuming_the_interrupted_attempt(tmp_path: Path) -> None:
    """Resuming an interrupted attempt would mix artifacts from separate invocations."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    with pytest.raises(RuntimeError):
        run_pair(
            _rendered(),
            run_root=tmp_path,
            pair_number=4,
            pair_id="pair-a",
            client=ScriptedModelClient([RuntimeError("boom")]),
            order_seed=1,
            attempt_id_factory=iter(("interrupted",)).__next__,
        )

    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 2),
        order_seed=1,
        attempt_id_factory=iter(("fresh",)).__next__,
    )

    assert pair.attempts[0].start.attempt_id == "fresh"
    assert (tmp_path / "pair-4-pair-a" / "attempts" / "interrupted").is_dir()


def test_existing_terminal_pair_summary_is_never_overwritten(tmp_path: Path) -> None:
    """Starting a completed pair again must preserve its terminal result byte-for-byte."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 2),
        order_seed=1,
        attempt_id_factory=iter(("first",)).__next__,
    )
    summary_path = tmp_path / "pair-4-pair-a" / "pair_summary.json"
    previous = summary_path.read_bytes()

    with pytest.raises(FileExistsError):
        run_pair(
            _rendered(),
            run_root=tmp_path,
            pair_number=4,
            pair_id="pair-a",
            client=ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 2),
            order_seed=1,
            attempt_id_factory=iter(("second",)).__next__,
        )

    assert summary_path.read_bytes() == previous


def test_openai_adapter_maps_request_and_exposes_typed_reply() -> None:
    """Changing adapter keywords or hiding provider status loses the shared reply boundary."""
    from dsx.pilot.runner import OpenAIModelClient

    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None
            self.with_raw_response = self

        def parse(self, **kwargs: object) -> object:
            self.kwargs = kwargs
            response = SimpleNamespace(
                output_parsed=_decision().model_dump(mode="json"),
                status="completed",
                error=None,
                incomplete_details=None,
                output=(),
            )
            return SimpleNamespace(
                parse=lambda: response,
                http_response=SimpleNamespace(text='{"status":"completed"}'),
            )

    responses = FakeResponses()
    request = _rendered().packet_on.request
    reply = OpenAIModelClient(SimpleNamespace(responses=responses)).complete(request)

    assert responses.kwargs is not None
    assert responses.kwargs["model"] == request.model_identifier
    assert responses.kwargs["instructions"] == request.system_prompt
    text_format = responses.kwargs["text_format"]
    assert isinstance(text_format, type)
    assert issubclass(text_format, AnalysisDecision)
    assert text_format.__name__ == request.response_schema_name
    assert responses.kwargs["store"] is False
    assert responses.kwargs["input"] == [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "context": request.context.model_dump(mode="json"),
                    "task_prompt": request.task_prompt,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
    ]
    assert reply.parsed_decision == _decision()
    assert reply.status == "completed"
    assert reply.raw_response is not None


def test_openai_adapter_uses_the_committed_response_schema_name_on_the_wire() -> None:
    """Ignoring the rendered schema name makes the request digest cosmetic at the SDK seam."""
    from dsx.pilot.runner import OpenAIModelClient

    captured: dict[str, object] = {}

    def respond(request: httpx2.Request) -> httpx2.Response:
        captured.update(json.loads(request.content))
        return httpx2.Response(
            200,
            json=_responses_payload("completed", _decision().model_dump_json()),
            request=request,
        )

    sdk = OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        max_retries=0,
    )
    request = _rendered().packet_off.request.model_copy(
        update={"response_schema_name": "custom_decision"}
    )

    reply = OpenAIModelClient(sdk).complete(request)

    text = captured["text"]
    assert isinstance(text, dict)
    response_format = text["format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    assert response_format["name"] == "custom_decision"
    assert response_format["strict"] is True
    assert isinstance(response_format["schema"], dict)
    assert reply.parsed_decision == _decision()


def test_openai_adapter_exposes_refusal_and_incomplete_data() -> None:
    """Discarding provider refusal or truncation fields breaks outcome classification."""
    from dsx.pilot.runner import OpenAIModelClient

    class FakeResponses:
        def __init__(self) -> None:
            self.with_raw_response = self

        def parse(self, **_: object) -> object:
            response = SimpleNamespace(
                output_parsed=None,
                status="incomplete",
                error=None,
                incomplete_details="max_output_tokens",
                output=(SimpleNamespace(content=(SimpleNamespace(refusal="not available"),)),),
            )
            return SimpleNamespace(
                parse=lambda: response,
                http_response=SimpleNamespace(text='{"status":"incomplete"}'),
            )

    reply = OpenAIModelClient(SimpleNamespace(responses=FakeResponses())).complete(
        _rendered().packet_off.request
    )

    assert reply.refusal == "not available"
    assert reply.incomplete_reason == "max_output_tokens"
    assert reply.parsed_decision is None


@pytest.mark.parametrize(
    ("status", "text", "expected"),
    [
        ("incomplete", '{"primary_metric":', OutcomeKind.incomplete),
        ("completed", "not valid JSON", OutcomeKind.invalid_output),
    ],
)
def test_real_openai_sdk_parse_failures_are_terminal_typed_outcomes(
    tmp_path: Path, status: str, text: str, expected: OutcomeKind
) -> None:
    """SDK structured parsing failures must not leave an interrupted pair attempt."""
    from dsx.pilot.runner import OpenAIModelClient, run_pair

    client = OpenAIModelClient(
        _sdk_client(_responses_payload(status, text), _responses_payload(status, text))
    )
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )

    assert pair.outcome_kind == "complete"
    assert tuple(outcome.outcome_kind for outcome in pair.attempts[0].outcomes) == (
        expected,
        expected,
    )
    assert all(outcome.raw_response is not None for outcome in pair.attempts[0].outcomes)


@pytest.mark.parametrize(
    "error_type",
    [
        AuthenticationError,
        PermissionDeniedError,
        NotFoundError,
        ConflictError,
        UnprocessableEntityError,
    ],
)
def test_openai_status_error_subclasses_are_provider_errors_with_raw_body(
    tmp_path: Path, error_type: type[APIStatusError]
) -> None:
    """Provider-status subclasses must retry and preserve their response body."""
    request = httpx2.Request("POST", "https://example.test/v1/responses")
    outer_response = '{"id":"resp-test","error":{"message":"denied"},"status":"failed"}'
    response = httpx2.Response(401, text=outer_response, request=request)
    error = error_type("denied", response=response, body={"message": "denied"})
    from dsx.pilot.runner import ScriptedModelClient, run_pair

    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=ScriptedModelClient([error] * 6),
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    first = pair.attempts[0].outcomes[0]
    assert first.outcome_kind is OutcomeKind.provider_error
    assert first.raw_response == outer_response


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            ModelReply(
                parsed_decision=_decision(),
                refusal="no",
                status="incomplete",
                incomplete_reason="budget",
            ),
            OutcomeKind.refused,
        ),
        (
            ModelReply(
                parsed_decision=_decision(),
                status="incomplete",
                incomplete_reason="budget",
            ),
            OutcomeKind.incomplete,
        ),
        (ModelReply(raw_response="{}"), OutcomeKind.invalid_output),
        (ModelReply(parsed_decision=_decision()), OutcomeKind.completed),
    ],
)
def test_non_provider_outcome_precedence(
    tmp_path: Path, reply: object, expected: OutcomeKind
) -> None:
    """Refusal, incompleteness, invalid output, and completion use the documented order."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    assert isinstance(reply, ModelReply)
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=ScriptedModelClient([reply, ModelReply(parsed_decision=_decision())]),
        order_seed=1,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )

    assert pair.attempts[0].outcomes[0].outcome_kind is expected


def test_second_arm_failure_discards_a_successful_first_arm_before_pair_rerun(
    tmp_path: Path,
) -> None:
    """A successful first-arm output must not be reused when the second arm invalidates it."""
    from dsx.pilot.runner import ModelReply, ModelTransportError, ScriptedModelClient, run_pair

    client = ScriptedModelClient(
        [
            ModelReply(parsed_decision=_decision()),
            ModelTransportError("offline"),
            ModelTransportError("offline"),
            ModelTransportError("offline"),
            ModelReply(parsed_decision=_decision()),
            ModelReply(parsed_decision=_decision()),
        ]
    )
    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=client,
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    assert pair.outcome_kind == "complete"
    assert tuple(outcome.outcome_kind for outcome in pair.attempts[0].outcomes) == (
        OutcomeKind.completed,
        OutcomeKind.transport_error,
        OutcomeKind.transport_error,
        OutcomeKind.transport_error,
    )
    assert sum(request.context.profile_packet is None for request in client.requests) == 2
    assert sum(request.context.profile_packet is not None for request in client.requests) == 4


def test_every_published_outcome_and_rendered_request_artifact_round_trips(tmp_path: Path) -> None:
    """Each append-only JSON artifact must be backed by its Pydantic contract."""
    from dsx.pilot.render import RenderedRequests
    from dsx.pilot.runner import ModelReply, ModelTransportError, ScriptedModelClient, run_pair

    rendered = _rendered()
    run_pair(
        rendered,
        run_root=tmp_path,
        pair_number=4,
        pair_id="pair-a",
        client=ScriptedModelClient(
            [
                ModelTransportError("offline"),
                ModelReply(parsed_decision=_decision()),
                ModelReply(refusal="no"),
            ]
        ),
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a",)).__next__,
    )
    attempt_dir = tmp_path / "pair-4-pair-a" / "attempts" / "attempt-a"

    assert (
        RenderedRequests.model_validate_json((attempt_dir / "rendered_requests.json").read_text())
        == rendered
    )
    outcomes = sorted((attempt_dir / "outcomes").glob("*.json"))
    assert len(outcomes) == 3
    assert all(ArmOutcome.model_validate_json(path.read_text()) for path in outcomes)


def test_openai_adapter_constructs_the_sdk_with_retries_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK-level retries would bypass the runner's recorded retry ledger."""
    import openai

    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    from dsx.pilot.runner import OpenAIModelClient

    OpenAIModelClient()

    assert captured == {"max_retries": 0}


def test_raw_envelope_and_adapter_scalar_conversion_paths_remain_observable() -> None:
    """Unusual provider envelope values must become durable strings or invalid output."""
    from dsx.pilot.runner import OpenAIModelClient, _reply_from_raw_envelope

    non_object_raw = SimpleNamespace(
        http_response=SimpleNamespace(text="[]", json=lambda: [])
    )
    assert _reply_from_raw_envelope(non_object_raw).raw_response == "[]"

    responses: list[object] = [
        SimpleNamespace(
            output_parsed=_decision(),
            status=123,
            error=ModelReply(status="failed"),
            refusal="direct refusal",
            incomplete_details=None,
            output=(),
        ),
        SimpleNamespace(
            output_parsed={"primary_metric": "not-valid"},
            status="completed",
            error=None,
            refusal=None,
            incomplete_details=None,
            output=(),
        ),
    ]

    class FakeResponses:
        def __init__(self) -> None:
            self.with_raw_response = self

        def parse(self, **_: object) -> object:
            response = responses.pop(0)
            return SimpleNamespace(
                parse=lambda: response,
                http_response=SimpleNamespace(text="{}"),
            )

    adapter = OpenAIModelClient(SimpleNamespace(responses=FakeResponses()))
    converted = adapter.complete(_rendered().packet_off.request)
    invalid = adapter.complete(_rendered().packet_off.request)

    assert converted.parsed_decision == _decision()
    assert converted.status == "123"
    assert converted.error == ModelReply(status="failed").model_dump_json()
    assert converted.refusal == "direct refusal"
    assert invalid.parsed_decision is None


def test_nested_response_refusal_scans_all_outputs_and_content_parts() -> None:
    """A refusal after empty provider content must not be missed."""
    from dsx.pilot.runner import _response_refusal

    response = SimpleNamespace(
        refusal=None,
        output=(
            SimpleNamespace(
                content=(SimpleNamespace(refusal=None), SimpleNamespace(refusal=None))
            ),
            SimpleNamespace(content=(SimpleNamespace(refusal="later refusal"),)),
        ),
    )

    assert _response_refusal(response) == "later refusal"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"refusal": "direct"}, "direct"),
        ({"output": "not-a-list"}, None),
        (
            {
                "output": [
                    "not-an-object",
                    {"content": "not-a-list"},
                    {"content": ["not-an-object", {"refusal": "nested"}]},
                ]
            },
            "nested",
        ),
    ],
)
def test_raw_payload_refusal_handles_every_provider_shape(
    payload: dict[str, object], expected: str | None
) -> None:
    """Malformed envelope containers must not crash or hide a later valid refusal."""
    from dsx.pilot.runner import _payload_refusal

    assert _payload_refusal(payload) == expected


@pytest.mark.parametrize("error", [TimeoutError("slow"), ConnectionError("lost")])
def test_builtin_transport_errors_use_the_recorded_retry_path(
    tmp_path: Path, error: Exception
) -> None:
    """Built-in timeout and connection failures must classify like SDK transport errors."""
    from dsx.pilot.runner import ModelReply, ScriptedModelClient, run_pair

    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=1,
        pair_id="pair",
        client=ScriptedModelClient(
            [error, error, error, ModelReply(parsed_decision=_decision())] * 2
        ),
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    assert pair.attempts[0].outcomes[0].outcome_kind is OutcomeKind.transport_error


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("raw body", "raw body"),
        ({"message": object()}, '{"message":"<object object at '),
        (None, None),
    ],
)
def test_status_errors_fall_back_to_each_available_body_shape(
    tmp_path: Path, body: object, expected: str | None
) -> None:
    """Empty HTTP text must preserve string or JSON provider bodies when available."""
    request = httpx2.Request("POST", "https://example.test/v1/responses")
    response = httpx2.Response(500, text="", request=request)
    error = APIStatusError("failed", response=response, body=body)
    from dsx.pilot.runner import ScriptedModelClient, run_pair

    pair = run_pair(
        _rendered(),
        run_root=tmp_path,
        pair_number=1,
        pair_id="pair",
        client=ScriptedModelClient([error] * 6),
        order_seed=1,
        sleep=lambda _: None,
        attempt_id_factory=iter(("attempt-a", "attempt-b")).__next__,
    )

    raw_response = pair.attempts[0].outcomes[0].raw_response
    if expected is None:
        assert raw_response is None
    else:
        assert raw_response is not None and raw_response.startswith(expected)
