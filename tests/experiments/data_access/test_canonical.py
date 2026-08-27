from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from dsx.experiments.data_access.canonical import (
    JsonPointerTraceAdapter,
    canonical_digest,
    canonical_json,
    parse_canonical_json,
    resolve_json_pointer,
)


def test_canonical_json_is_sorted_compact_and_rejects_nonfinite_values() -> None:
    assert canonical_json({"b": [True, None], "a": "é"}) == '{"a":"é","b":[true,null]}'
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})
    with pytest.raises(ValueError, match="canonical"):
        canonical_json({"bad": float("nan")})


def test_canonical_packet_parse_and_json_pointer_resolution() -> None:
    payload = canonical_json({"a/b": [{"~key": "value"}]})
    parsed = parse_canonical_json(payload)
    assert resolve_json_pointer(parsed, "/a~1b/0/~0key") == "value"
    assert JsonPointerTraceAdapter().resolve(parsed, "/a~1b/0/~0key") == "value"
    assert resolve_json_pointer(parsed, "") == parsed
    with pytest.raises(ValueError, match="pointer"):
        resolve_json_pointer(parsed, "a")
    with pytest.raises(ValueError, match="not canonical"):
        parse_canonical_json('{"b":1,"a":2}')


def test_canonical_value_conversions_and_invalid_pointer_forms(tmp_path: Path) -> None:
    value = {
        "date": date(2026, 8, 26),
        "datetime": datetime(2026, 8, 26, 1, 2, 3),
        "time": time(1, 2, 3),
        "decimal": Decimal("1.20"),
        "path": tmp_path,
        "bytes": b"\x00",
    }
    rendered = canonical_json(value)
    assert '"decimal":"1.20"' in rendered
    assert '"$binary_hex":"00"' in rendered
    with pytest.raises(ValueError, match="cannot be canonicalized"):
        canonical_json({"set": {1}})
    with pytest.raises(ValueError, match="invalid"):
        parse_canonical_json("{")
    for pointer in ("/missing", "/a/not-number", "/a/2", "/a/0/value"):
        with pytest.raises(ValueError):
            resolve_json_pointer({"a": ["value"]}, pointer)
