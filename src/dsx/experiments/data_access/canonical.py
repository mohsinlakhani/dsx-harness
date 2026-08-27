"""Canonical JSON and committed opaque-packet helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


def json_value(value: Any) -> Any:
    """Convert supported values to a JSON-compatible value without losing structure."""
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"$binary_hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON contains a non-finite float")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError(f"canonical JSON does not support {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return compact, sorted, UTF-8-safe canonical JSON for a JSON-compatible value."""
    try:
        return json.dumps(
            json_value(value),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value cannot be canonicalized as JSON") from error


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 digest of canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_byte_count(value: Any) -> int:
    """Return the UTF-8 byte count of canonical JSON."""
    return len(canonical_json(value).encode("utf-8"))


def parse_canonical_json(payload: str) -> Any:
    """Parse a committed packet only when it is itself canonical JSON."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("packet JSON is invalid") from error
    if canonical_json(parsed) != payload:
        raise ValueError("packet JSON is not canonical")
    return parsed


def resolve_json_pointer(payload: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 pointer from a committed JSON packet."""
    if pointer == "":
        return payload
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or begin with '/'")
    current = payload
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError("JSON pointer does not resolve")
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, str):
            if not token.isdecimal():
                raise ValueError("array JSON pointer token must be a non-negative integer")
            index = int(token)
            if index >= len(current):
                raise ValueError("JSON pointer does not resolve")
            current = current[index]
        else:
            raise ValueError("JSON pointer does not resolve")
    return current


class PacketTraceAdapter(Protocol):
    """Resolve packet evidence without imposing a schema on the packet itself."""

    def resolve(self, packet_payload: Any, reference: str) -> Any:
        """Resolve one evidence reference from an already committed packet payload."""


class JsonPointerTraceAdapter:
    """V1 packet tracing based on RFC 6901 pointers."""

    def resolve(self, packet_payload: Any, reference: str) -> Any:
        return resolve_json_pointer(packet_payload, reference)
