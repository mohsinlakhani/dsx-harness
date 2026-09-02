"""Tests for DSX Packet envelope contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from dsx.packet import DatasetRef, DsxPacket, PacketModule


def _module(**changes: object) -> PacketModule:
    values: dict[str, object] = {
        "module_id": "population",
        "module_type": "profile.population",
        "schema_version": "1",
        "content": {"rows": 5000},
        "evidence_refs": ("query:row-count",),
        "tags": ("profile",),
    }
    values.update(changes)
    return PacketModule.model_validate(values)


def _packet(*modules: PacketModule) -> DsxPacket:
    return DsxPacket(
        packet_id="credit-review-2026",
        dataset=DatasetRef(digest="a" * 64, name="credit-review"),
        modules=modules or (_module(),),
    )


def test_packet_supports_versioned_modules_with_arbitrary_json_content() -> None:
    packet = _packet(
        _module(),
        _module(
            module_id="feature-risks",
            module_type="risk.features",
            schema_version="2",
            content=[{"column": "row_id", "risk": "identifier"}],
            evidence_refs=("query:uniqueness:row_id",),
            tags=("feature-review", "modeling"),
        ),
    )

    restored = DsxPacket.model_validate_json(packet.model_dump_json())

    assert restored == packet
    assert restored.modules[1].content == [{"column": "row_id", "risk": "identifier"}]


def test_packet_canonical_json_and_digest_are_deterministic() -> None:
    packet = _packet()
    parsed = json.loads(packet.canonical_json())

    assert parsed["schema_version"] == "dsx-packet/v1"
    assert packet.digest() == _packet().digest()
    assert len(packet.digest()) == 64


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"evidence_refs": ("same", "same")}, "evidence_refs must be unique"),
        ({"tags": ("same", "same")}, "tags must be unique"),
    ],
)
def test_module_rejects_duplicate_references(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _module(**changes)


def test_packet_rejects_duplicate_module_ids() -> None:
    with pytest.raises(ValidationError, match="module_id must be unique"):
        _packet(_module(), _module(module_type="profile.columns"))


def test_module_rejects_nonstandard_json_numbers() -> None:
    with pytest.raises(ValidationError, match="content must be standard JSON"):
        _module(content=float("nan"))
