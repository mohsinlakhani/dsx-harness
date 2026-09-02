"""Tests for task-scoped DSX Packet assembly."""

from __future__ import annotations

import pytest

from dsx.packet import (
    DatasetRef,
    DsTask,
    DsxPacket,
    PacketModule,
    assemble_task_packet,
)


def _packet() -> DsxPacket:
    return DsxPacket(
        packet_id="dataset-v1",
        dataset=DatasetRef(digest="b" * 64),
        modules=(
            PacketModule(
                module_id="population",
                module_type="profile.population",
                schema_version="1",
                content={"rows": 100},
            ),
            PacketModule(
                module_id="feature-risks",
                module_type="risk.features",
                schema_version="1",
                content={"likely_ids": ["row_id"]},
            ),
            PacketModule(
                module_id="split-risks",
                module_type="risk.split",
                schema_version="1",
                content={"entity_column": "account_id"},
            ),
        ),
    )


def test_task_packet_selects_declared_types_and_binds_source() -> None:
    source = _packet()
    task = DsTask(
        task_id="feature-review-1",
        task_type="feature-review",
        objective="Review candidate features for leakage.",
        module_types=("profile.population", "risk.features", "risk.features"),
    )

    selected = assemble_task_packet(source, task)

    assert task.unique_module_types == ("profile.population", "risk.features")
    assert [module.module_id for module in selected.modules] == ["population", "feature-risks"]
    assert selected.source_packet_id == source.packet_id
    assert selected.source_packet_digest == source.digest()
    assert selected.dataset_digest == source.dataset.digest


def test_task_packet_rejects_a_missing_required_module_type() -> None:
    task = DsTask(
        task_id="monitoring-1",
        task_type="monitoring-review",
        objective="Plan model monitoring.",
        module_types=("profile.population", "monitoring.drift"),
    )

    with pytest.raises(
        ValueError,
        match="packet does not contain required module types: monitoring.drift",
    ):
        assemble_task_packet(_packet(), task)
