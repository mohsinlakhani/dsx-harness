"""Stable, extensible contracts for the branded DSX Packet envelope."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ModuleName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"),
]
PacketId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]
Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class PacketContract(BaseModel):
    """Strict immutable base for public packet contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetRef(PacketContract):
    """Identity of the dataset snapshot described by a packet."""

    digest: Sha256Digest
    algorithm: Literal["sha256"] = "sha256"
    name: str | None = Field(default=None, min_length=1)


class PacketModule(PacketContract):
    """One independently versioned unit of context in a DSX Packet.

    ``content`` intentionally accepts any JSON value. A module owner can evolve or
    validate that payload independently while the packet envelope remains stable.
    """

    module_id: ModuleName
    module_type: ModuleName
    schema_version: str = Field(min_length=1, max_length=64)
    content: JsonValue
    evidence_refs: tuple[str, ...] = ()
    tags: tuple[ModuleName, ...] = ()

    @model_validator(mode="after")
    def validate_unique_references(self) -> PacketModule:
        try:
            json.dumps(self.content, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("content must be standard JSON") from error
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs must be unique within a module")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique within a module")
        return self


class DsxPacket(PacketContract):
    """A versioned collection of independently extensible DSX context modules."""

    schema_version: Literal["dsx-packet/v1"] = "dsx-packet/v1"
    packet_id: PacketId
    dataset: DatasetRef
    modules: tuple[PacketModule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_module_ids(self) -> DsxPacket:
        module_ids = [module.module_id for module in self.modules]
        if len(set(module_ids)) != len(module_ids):
            raise ValueError("module_id must be unique within a packet")
        return self

    def canonical_json(self) -> str:
        """Return deterministic JSON suitable for persistence and commitments."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        """Return the SHA-256 commitment for the canonical packet."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
