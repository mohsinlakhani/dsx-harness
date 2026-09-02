"""Deterministic warning-only feature-risk detectors."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .models import ColumnsProfile, FeatureRiskFinding


def detect_likely_identifiers(
    profile: ColumnsProfile,
    *,
    target_column: str,
    evidence_refs: Sequence[str],
) -> tuple[FeatureRiskFinding, ...]:
    """Return exact fully populated non-target identifier candidates in source order."""
    unique_refs = tuple(dict.fromkeys(evidence_refs))
    findings: list[FeatureRiskFinding] = []
    for column in profile.columns:
        if column.name == target_column:
            continue
        if column.non_null_count != profile.row_count:
            continue
        if column.distinct_count != profile.row_count:
            continue
        quoted_name = json.dumps(column.name, ensure_ascii=False)
        findings.append(
            FeatureRiskFinding(
                finding_id=f"likely-identifier:{column.name}",
                kind="likely_identifier",
                severity="warning",
                column=column.name,
                message=(
                    f"Column {quoted_name} has one distinct non-null value per row "
                    "and may be an identifier."
                ),
                row_count=profile.row_count,
                non_null_count=column.non_null_count,
                distinct_count=column.distinct_count,
                uniqueness_rate=column.uniqueness_rate,
                recommended_action="exclude_or_verify",
                evidence_refs=unique_refs,
            )
        )
    return tuple(findings)
