"""Post-reveal uptake diagnostics for builder-generated Data Access packets."""

from __future__ import annotations

import math
import re

from dsx.builders.models import (
    COLUMN_PROFILE_MODULE_ID,
    DATA_TRAPS_MODULE_ID,
    FEATURE_RISKS_MODULE_ID,
    ColumnsProfile,
    FeatureRisks,
)
from dsx.packet import DsxPacket

from .evaluation import DataAccessDecision, EvidenceKind
from .models import Arm, DataAccessContract
from .realistic import _data_traps, packet_module_content

_MODULE_POINTER = re.compile(r"^/modules/(\d+)(?:/|$)")
_PACKET_ARMS = frozenset({Arm.dsx_packet, Arm.packet_and_full_data})


class IdentifierUptake(DataAccessContract):
    columns: tuple[str, ...]
    excluded: tuple[str, ...]
    missed: tuple[str, ...]
    all_excluded: bool


class ImbalanceUptake(DataAccessContract):
    applicable: bool
    acknowledged: bool | None


class PacketModuleCitations(DataAccessContract):
    column_profile: bool
    feature_risks: bool
    data_traps: bool


class UptakeRecord(DataAccessContract):
    arm: Arm
    repetition_id: str
    identifiers: IdentifierUptake
    imbalance: ImbalanceUptake
    packet_module_citations: PacketModuleCitations | None


def identifier_columns_for_arm(
    packet: DsxPacket, *, arm: Arm, target_column: str
) -> tuple[str, ...]:
    """Return identifier columns using the arm's committed truth source."""
    if arm in _PACKET_ARMS:
        feature_risks = packet_module_content(packet, FEATURE_RISKS_MODULE_ID, FeatureRisks)
        return tuple(
            finding.column
            for finding in feature_risks.findings
            if finding.kind == "likely_identifier"
        )
    profile = packet_module_content(packet, COLUMN_PROFILE_MODULE_ID, ColumnsProfile)
    return tuple(
        column.name
        for column in profile.columns
        if column.name != target_column
        and math.isclose(column.uniqueness_rate, 1.0, rel_tol=1e-12, abs_tol=1e-12)
    )


def module_id_for_pointer(packet: DsxPacket, pointer: str) -> str | None:
    """Map `/modules/<int>...` to a module_id, or None when the pointer is invalid."""
    match = _MODULE_POINTER.match(pointer)
    if match is None:
        return None
    index = int(match.group(1))
    if index >= len(packet.modules):
        return None
    return packet.modules[index].module_id


def evaluate_uptake(
    decision: DataAccessDecision,
    packet: DsxPacket,
    *,
    arm: Arm,
    repetition_id: str,
    target_column: str,
) -> UptakeRecord:
    """Score identifier exclusion, imbalance acknowledgement, and packet citations."""
    columns = identifier_columns_for_arm(packet, arm=arm, target_column=target_column)
    excluded_set = set(decision.excluded_columns)
    excluded = tuple(column for column in columns if column in excluded_set)
    missed = tuple(column for column in columns if column not in excluded_set)
    identifiers = IdentifierUptake(
        columns=columns,
        excluded=excluded,
        missed=missed,
        all_excluded=not missed,
    )

    traps = _data_traps(packet)
    applicable = any(trap.kind == "target_class_imbalance" for trap in traps)
    if applicable:
        haystack = "".join(
            (decision.reasoning, decision.recommendation, *decision.limitations)
        ).lower()
        acknowledged: bool | None = "imbalance" in haystack
    else:
        acknowledged = None
    imbalance = ImbalanceUptake(applicable=applicable, acknowledged=acknowledged)

    citations: PacketModuleCitations | None = None
    if arm is not Arm.full_data:
        cited: set[str] = set()
        for claim in decision.factual_claims:
            for evidence in claim.evidence:
                if evidence.kind is not EvidenceKind.packet_json_pointer:
                    continue
                module_id = module_id_for_pointer(packet, evidence.pointer)
                if module_id is not None:
                    cited.add(module_id)
        citations = PacketModuleCitations(
            column_profile=COLUMN_PROFILE_MODULE_ID in cited,
            feature_risks=FEATURE_RISKS_MODULE_ID in cited,
            data_traps=DATA_TRAPS_MODULE_ID in cited,
        )

    return UptakeRecord(
        arm=arm,
        repetition_id=repetition_id,
        identifiers=identifiers,
        imbalance=imbalance,
        packet_module_citations=citations,
    )
