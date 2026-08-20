"""Tests for deterministic controlled-arm request rendering."""

from __future__ import annotations

import math
import warnings

import pytest

from dsx.pilot.models import (
    Arm,
    ModelRequest,
    RequestContext,
    candidate_packet,
    generate_pilot_case,
)
from dsx.pilot.render import (
    RequestConfiguration,
    canonicalize_request,
    prove_controlled_delta,
    render_requests,
    request_digest,
)


def _configuration() -> RequestConfiguration:
    return RequestConfiguration(
        model_identifier="gpt-test",
        system_prompt="Return the requested structured analysis.",
        response_schema_name="analysis_decision",
    )


def test_rendered_arms_share_configuration_and_differ_only_in_packet() -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())

    assert tuple(arm.arm for arm in rendered.arms) == (Arm.packet_off, Arm.packet_on)
    off = rendered.packet_off.request.model_dump(mode="json")
    on = rendered.packet_on.request.model_dump(mode="json")
    assert off["task_prompt"] == on["task_prompt"]
    assert off["context"]["profile_packet"] is None
    assert on["context"]["profile_packet"] == candidate_packet().model_dump(mode="json")
    assert off["context"]["profile_packet"] != on["context"]["profile_packet"]
    assert rendered.packet_off.request_digest != rendered.packet_on.request_digest
    assert (
        rendered.packet_off.common_projection_digest
        == rendered.packet_on.common_projection_digest
    )
    assert rendered.common_projection_digest == rendered.packet_off.common_projection_digest


def test_canonicalization_is_sorted_compact_utf8_json() -> None:
    request = ModelRequest(
        model_identifier="gpt-test",
        system_prompt="System",
        task_prompt="Task",
        response_schema_name="analysis_decision",
        context=RequestContext(profile_packet=None),
    )

    expected = (
        '{"context":{"profile_packet":null},"model_identifier":"gpt-test",'
        '"response_schema_name":"analysis_decision","system_prompt":"System",'
        '"task_prompt":"Task"}'
    )
    expected_digest = "e8d325731e8a7097b7442b697266d0b2c830fe1faecd43d32e79adacd9fbc8f8"
    assert canonicalize_request(request) == expected
    assert request_digest(request) == expected_digest
    assert canonicalize_request(
        {"context": {"profile_packet": None}, "task_prompt": "Task", "system_prompt": "System",
         "response_schema_name": "analysis_decision", "model_identifier": "gpt-test"}
    ) == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonicalization_rejects_non_finite_floats(value: float) -> None:
    """Allowing NaN or infinity would emit non-standard request JSON."""
    with pytest.raises(ValueError, match="non-finite"):
        canonicalize_request({"value": value})


def test_proof_rejects_each_protected_field_change() -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())
    protected_fields = {
        "model_identifier": "other-model",
        "system_prompt": "other-system",
        "task_prompt": "other-task",
        "response_schema_name": "other-schema",
    }

    for field, value in protected_fields.items():
        changed = rendered.packet_on.request.model_copy(update={field: value})
        with pytest.raises(ValueError, match=field):
            prove_controlled_delta(rendered.packet_off.request, changed)


def test_proof_rejects_non_packet_nested_changes_and_names_path() -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())
    changed = {
        **rendered.packet_on.request.model_dump(mode="json"),
        "context": {
            "profile_packet": candidate_packet().model_dump(mode="json"),
            "unexpected": True,
        },
    }

    with pytest.raises(ValueError, match="context.unexpected"):
        prove_controlled_delta(rendered.packet_off.request, changed)


@pytest.mark.parametrize("mutation", ["remove", "change"])
def test_proof_rejects_non_packet_nested_removal_or_change(mutation: str) -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())
    off = rendered.packet_off.request.model_dump(mode="json")
    on = rendered.packet_on.request.model_dump(mode="json")
    off["context"]["nested"] = "same"
    on["context"]["nested"] = "same"
    if mutation == "remove":
        del on["context"]["nested"]
    else:
        on["context"]["nested"] = "changed"

    with pytest.raises(ValueError, match="context.nested"):
        prove_controlled_delta(off, on)


def test_proof_rejects_deleted_packet_off_path() -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())
    off = rendered.packet_off.request.model_dump(mode="json")
    del off["context"]["profile_packet"]

    with pytest.raises(ValueError, match="context.profile_packet"):
        prove_controlled_delta(off, rendered.packet_on.request)


def test_proof_rejects_deleted_packet_on_path() -> None:
    """The treatment request must explicitly carry the committed packet field."""
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())
    on = rendered.packet_on.request.model_dump(mode="json")
    del on["context"]["profile_packet"]

    with pytest.raises(ValueError, match="packet_on must contain"):
        prove_controlled_delta(rendered.packet_off.request, on)


def test_proof_rejects_a_packet_different_from_the_supplied_commitment() -> None:
    """Matching common fields cannot substitute a different treatment packet."""
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())
    expected = candidate_packet().model_copy(update={"version": "different"})

    with pytest.raises(ValueError, match="does not contain supplied packet"):
        prove_controlled_delta(
            rendered.packet_off.request,
            rendered.packet_on.request,
            expected_packet=expected,
        )


def test_proof_rejects_non_mapping_contexts_as_missing_packet_paths() -> None:
    """A malformed context container must fail as a missing explicit treatment field."""
    with pytest.raises(ValueError, match="profile_packet"):
        prove_controlled_delta({"context": "invalid"}, {"context": "invalid"})


@pytest.mark.parametrize(
    ("off_items", "on_items", "path"),
    [([1], [1, 2], "items.1"), ([1, 2], [1], "items.1"), ([1], [2], "items.0")],
)
def test_proof_names_list_length_and_value_differences(
    off_items: list[int], on_items: list[int], path: str
) -> None:
    """Nested list drift must identify the exact index regardless of which side changed."""
    packet = candidate_packet().model_dump(mode="json")
    off = {"context": {"profile_packet": None}, "items": off_items}
    on = {"context": {"profile_packet": packet}, "items": on_items}

    with pytest.raises(ValueError, match=path):
        prove_controlled_delta(off, on, expected_packet=candidate_packet())


def test_proof_accepts_equal_empty_shared_lists() -> None:
    """An empty shared list is part of the protected common projection, not a delta."""
    packet = candidate_packet().model_dump(mode="json")
    off = {"context": {"profile_packet": None}, "items": []}
    on = {"context": {"profile_packet": packet}, "items": []}

    assert prove_controlled_delta(off, on, expected_packet=candidate_packet())


def test_proof_rejects_incorrect_packet_arm_states() -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())

    with pytest.raises(ValueError, match="context.profile_packet"):
        prove_controlled_delta(rendered.packet_on.request, rendered.packet_off.request)


def test_large_packet_warns_without_rejecting() -> None:
    case = generate_pilot_case(seed=7)
    packet = candidate_packet().model_copy(update={"metric_guidance": "x" * (32 * 1024 + 1)})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rendered = render_requests(case, packet, _configuration())

    assert rendered.packet_on.request.context.profile_packet == packet
    assert any("32 KiB" in str(item.message) for item in caught)


def test_rendered_requests_are_immutable() -> None:
    rendered = render_requests(generate_pilot_case(seed=7), candidate_packet(), _configuration())

    with pytest.raises((TypeError, ValueError)):
        rendered.packet_off.request_digest = "changed"  # type: ignore[misc]
