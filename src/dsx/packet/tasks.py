"""Task-specific views over a reusable DSX Packet."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import (
    DsxPacket,
    ModuleName,
    PacketContract,
    PacketId,
    PacketModule,
    Sha256Digest,
)


class DsTask(PacketContract):
    """A data-science task and the packet module types it requires."""

    task_id: PacketId
    task_type: ModuleName
    objective: str = Field(min_length=1)
    module_types: tuple[ModuleName, ...] = Field(min_length=1)

    @property
    def unique_module_types(self) -> tuple[str, ...]:
        """Return requested module types once, preserving declared order."""
        return tuple(dict.fromkeys(self.module_types))


class TaskPacket(PacketContract):
    """A task-scoped projection that remains bound to its source DSX Packet."""

    schema_version: Literal["dsx-task-packet/v1"] = "dsx-task-packet/v1"
    task: DsTask
    source_packet_id: PacketId
    source_packet_digest: Sha256Digest
    dataset_digest: Sha256Digest
    modules: tuple[PacketModule, ...] = Field(min_length=1)


def assemble_task_packet(packet: DsxPacket, task: DsTask) -> TaskPacket:
    """Select all modules requested by a task, failing if a type is unavailable."""
    requested = set(task.unique_module_types)
    available = {module.module_type for module in packet.modules}
    missing = requested - available
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"packet does not contain required module types: {names}")

    selected = tuple(module for module in packet.modules if module.module_type in requested)
    return TaskPacket(
        task=task,
        source_packet_id=packet.packet_id,
        source_packet_digest=packet.digest(),
        dataset_digest=packet.dataset.digest,
        modules=selected,
    )
