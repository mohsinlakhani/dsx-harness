"""Deterministic rendering and proof of Context Lift's controlled request pair."""

from __future__ import annotations

import copy
import hashlib
import json
import warnings
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from .models import (
    Arm,
    ModelRequest,
    Packet,
    PilotCase,
    PilotContract,
    RequestContext,
    ResponseSchemaName,
)

_MAX_PACKET_BYTES = 32 * 1024
_MISSING = object()


class RequestConfiguration(PilotContract):
    """Immutable request fields shared by both treatment arms."""

    model_identifier: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    response_schema_name: ResponseSchemaName


RequestConfig = RequestConfiguration


class RenderedArm(PilotContract):
    """One immutable, fully rendered request and its proof metadata."""

    arm: Arm
    request: ModelRequest
    request_digest: str = Field(min_length=64, max_length=64)
    common_projection_digest: str = Field(min_length=64, max_length=64)

    @property
    def label(self) -> Arm:
        """Return the arm label in the form expected by the later runner."""
        return self.arm


class RenderedRequests(PilotContract):
    """The deterministic packet-off, packet-on pair plus its shared proof."""

    packet_off: RenderedArm
    packet_on: RenderedArm
    common_projection_digest: str = Field(min_length=64, max_length=64)

    @property
    def arms(self) -> tuple[RenderedArm, RenderedArm]:
        """Return arms in deterministic packet-off then packet-on order."""
        return (self.packet_off, self.packet_on)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonicalize_request(request: BaseModel | Mapping[str, Any]) -> str:
    """Return compact, sorted-key JSON for the exact request parameters."""
    payload = _json_value(request)
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except ValueError as error:
        raise ValueError("canonical request JSON contains a non-finite float") from error


def request_digest(request: BaseModel | Mapping[str, Any]) -> str:
    """Return the SHA-256 digest of a canonical request projection."""
    return hashlib.sha256(canonicalize_request(request).encode("utf-8")).hexdigest()


def _without_packet(payload: Mapping[str, Any]) -> dict[str, Any]:
    projection = copy.deepcopy(dict(payload))
    context = projection.get("context")
    if isinstance(context, dict):
        context.pop("profile_packet", None)
    return projection


def _path_text(path: tuple[str, ...]) -> str:
    return ".".join(path) or "<root>"


def _differences(left: Any, right: Any, path: tuple[str, ...] = ()) -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = path + (str(key),)
            if key not in left or key not in right:
                paths.append(_path_text(child))
            else:
                paths.extend(_differences(left[key], right[key], child))
        return paths
    if isinstance(left, list) and isinstance(right, list):
        paths = []
        for index in range(max(len(left), len(right))):
            child = path + (str(index),)
            if index >= len(left) or index >= len(right):
                paths.append(_path_text(child))
            else:
                paths.extend(_differences(left[index], right[index], child))
        return paths
    if left != right:
        return [_path_text(path)]
    return []


def _common_digest(payload: Mapping[str, Any]) -> str:
    return request_digest(_without_packet(payload))


def _profile_packet(payload: Mapping[str, Any]) -> Any:
    context = payload.get("context")
    if isinstance(context, Mapping):
        return context["profile_packet"] if "profile_packet" in context else _MISSING
    return _MISSING


def prove_controlled_delta(
    packet_off: ModelRequest | Mapping[str, Any],
    packet_on: ModelRequest | Mapping[str, Any],
    expected_packet: Packet | None = None,
) -> str:
    """Prove that only ``context.profile_packet`` differs and return its digest.

    The returned digest is computed from both complete projections after removing
    the one allowlisted packet path. Any protected or structural difference is
    reported before a caller can pass either request to a client.
    """
    off_payload = _json_value(packet_off)
    on_payload = _json_value(packet_on)
    off_packet = _profile_packet(off_payload)
    on_packet = _profile_packet(on_payload)
    off_common = _common_digest(off_payload)
    on_common = _common_digest(on_payload)
    differences = _differences(_without_packet(off_payload), _without_packet(on_payload))

    if off_packet is _MISSING:
        differences.append("context.profile_packet (packet_off must explicitly contain None)")
    elif off_packet is not None:
        differences.append("context.profile_packet (packet_off must be None)")
    if on_packet is _MISSING:
        differences.append("context.profile_packet (packet_on must contain the packet)")
    elif on_packet is None:
        differences.append("context.profile_packet (packet_on must contain the packet)")
    if expected_packet is not None and on_packet != _json_value(expected_packet):
        differences.append("context.profile_packet (packet_on does not contain supplied packet)")

    if differences or off_common != on_common:
        paths = ", ".join(dict.fromkeys(differences)) or "<canonical projection>"
        raise ValueError(
            "controlled request proof failed at JSON path(s) "
            f"{paths}; common-projection digests: packet_off={off_common}, "
            f"packet_on={on_common}"
        )
    return off_common


def render_requests(
    case: PilotCase,
    packet: Packet,
    configuration: RequestConfiguration,
) -> RenderedRequests:
    """Render and prove packet-off/on requests in deterministic arm order."""
    packet_payload = canonicalize_request(packet)
    if len(packet_payload.encode("utf-8")) > _MAX_PACKET_BYTES:
        warnings.warn(
            "canonical profile packet payload exceeds 32 KiB; continuing without rejection",
            UserWarning,
            stacklevel=2,
        )

    shared = {
        "model_identifier": configuration.model_identifier,
        "system_prompt": configuration.system_prompt,
        "task_prompt": case.task_text,
        "response_schema_name": configuration.response_schema_name,
    }
    packet_off = ModelRequest(**shared, context=RequestContext(profile_packet=None))
    packet_on = ModelRequest(**shared, context=RequestContext(profile_packet=packet))
    common_digest = prove_controlled_delta(packet_off, packet_on, expected_packet=packet)
    return RenderedRequests(
        packet_off=RenderedArm(
            arm=Arm.packet_off,
            request=packet_off,
            request_digest=request_digest(packet_off),
            common_projection_digest=common_digest,
        ),
        packet_on=RenderedArm(
            arm=Arm.packet_on,
            request=packet_on,
            request_digest=request_digest(packet_on),
            common_projection_digest=common_digest,
        ),
        common_projection_digest=common_digest,
    )
