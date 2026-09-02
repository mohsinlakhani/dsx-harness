"""Tests for exact likely-identifier feature-risk detection."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from dsx.builders.feature_risks import detect_likely_identifiers
from dsx.builders.models import ColumnCardinality, ColumnsProfile, FeatureRiskFinding, FeatureRisks


def _column(
    name: str,
    *,
    row_count: int,
    non_null_count: int,
    distinct_count: int,
    duckdb_type: str = "VARCHAR",
) -> ColumnCardinality:
    return ColumnCardinality(
        name=name,
        duckdb_type=duckdb_type,
        non_null_count=non_null_count,
        distinct_count=distinct_count,
        uniqueness_rate=distinct_count / row_count,
    )


def _profile(
    *columns: ColumnCardinality,
    row_count: int = 3,
    snapshot_id: str = "current",
) -> ColumnsProfile:
    return ColumnsProfile(
        current_snapshot_id=snapshot_id,
        row_count=row_count,
        columns=columns,
    )


def _exact_finding(
    column: str,
    *,
    row_count: int = 3,
    finding_id: str | None = None,
    evidence_refs: tuple[str, ...] = ("sha256:aaa",),
) -> FeatureRiskFinding:
    quoted = json.dumps(column, ensure_ascii=False)
    return FeatureRiskFinding(
        finding_id=finding_id or f"likely-identifier:{column}",
        kind="likely_identifier",
        severity="warning",
        column=column,
        message=(
            f"Column {quoted} has one distinct non-null value per row and may be an identifier."
        ),
        row_count=row_count,
        non_null_count=row_count,
        distinct_count=row_count,
        uniqueness_rate=1.0,
        recommended_action="exclude_or_verify",
        evidence_refs=evidence_refs,
    )


def _fixture_profile() -> ColumnsProfile:
    return _profile(
        _column("row_id", row_count=3, non_null_count=3, distinct_count=3, duckdb_type="BIGINT"),
        _column("duplicate_value", row_count=3, non_null_count=3, distinct_count=2),
        _column("nullable_unique", row_count=3, non_null_count=2, distinct_count=2),
        _column("constant_value", row_count=3, non_null_count=3, distinct_count=1),
        _column("all_null", row_count=3, non_null_count=0, distinct_count=0),
        _column("label", row_count=3, non_null_count=3, distinct_count=3),
    )


def test_detects_exact_row_id_and_skips_non_identifiers() -> None:
    findings = detect_likely_identifiers(
        _fixture_profile(),
        target_column="label",
        evidence_refs=("sha256:aaa",),
    )
    assert [finding.column for finding in findings] == ["row_id"]
    assert findings[0].finding_id == "likely-identifier:row_id"
    assert findings[0].kind == "likely_identifier"
    assert findings[0].severity == "warning"
    assert findings[0].recommended_action == "exclude_or_verify"
    assert findings[0].row_count == findings[0].non_null_count == 3
    assert findings[0].distinct_count == 3
    assert findings[0].uniqueness_rate == 1.0
    assert findings[0].message == (
        'Column "row_id" has one distinct non-null value per row and may be an identifier.'
    )


def test_one_row_dataset_still_matches_exact_identifier_rule() -> None:
    profile = _profile(
        _column("row_id", row_count=1, non_null_count=1, distinct_count=1),
        _column("label", row_count=1, non_null_count=1, distinct_count=1),
        row_count=1,
    )
    findings = detect_likely_identifiers(
        profile,
        target_column="label",
        evidence_refs=("sha256:aaa",),
    )
    assert [finding.column for finding in findings] == ["row_id"]
    assert findings[0].row_count == 1
    assert findings[0].uniqueness_rate == 1.0


def test_two_matching_non_target_columns_preserve_source_order() -> None:
    profile = _profile(
        _column("row_id", row_count=3, non_null_count=3, distinct_count=3),
        _column("duplicate_value", row_count=3, non_null_count=3, distinct_count=2),
        _column("other_id", row_count=3, non_null_count=3, distinct_count=3),
        _column("label", row_count=3, non_null_count=3, distinct_count=3),
    )
    findings = detect_likely_identifiers(
        profile,
        target_column="label",
        evidence_refs=("sha256:aaa",),
    )
    assert [finding.column for finding in findings] == ["row_id", "other_id"]
    assert [finding.finding_id for finding in findings] == [
        "likely-identifier:row_id",
        "likely-identifier:other_id",
    ]


def test_matching_ignores_column_type_and_identifier_like_names() -> None:
    profile = _profile(
        _column(
            "not_an_id",
            row_count=3,
            non_null_count=3,
            distinct_count=3,
            duckdb_type="INTEGER",
        ),
        _column("id", row_count=3, non_null_count=3, distinct_count=2, duckdb_type="VARCHAR"),
        _column("label", row_count=3, non_null_count=3, distinct_count=3),
    )
    findings = detect_likely_identifiers(
        profile,
        target_column="label",
        evidence_refs=("sha256:aaa",),
    )
    assert [finding.column for finding in findings] == ["not_an_id"]


def test_quoted_unicode_column_has_deterministic_id_and_message() -> None:
    name = 'id "列"'
    profile = _profile(
        _column(name, row_count=3, non_null_count=3, distinct_count=3),
        _column("label", row_count=3, non_null_count=3, distinct_count=3),
    )
    findings = detect_likely_identifiers(
        profile,
        target_column="label",
        evidence_refs=("sha256:aaa",),
    )
    assert len(findings) == 1
    quoted = json.dumps(name, ensure_ascii=False)
    assert findings[0].finding_id == f"likely-identifier:{name}"
    assert findings[0].column == name
    assert findings[0].message == (
        f"Column {quoted} has one distinct non-null value per row and may be an identifier."
    )


def test_profile_with_no_candidates_returns_empty_tuple() -> None:
    profile = _profile(
        _column("duplicate_value", row_count=3, non_null_count=3, distinct_count=2),
        _column("label", row_count=3, non_null_count=3, distinct_count=3),
    )
    assert (
        detect_likely_identifiers(
            profile,
            target_column="label",
            evidence_refs=("sha256:aaa",),
        )
        == ()
    )


def test_evidence_refs_are_copied_and_deduplicated_in_first_seen_order() -> None:
    findings = detect_likely_identifiers(
        _fixture_profile(),
        target_column="label",
        evidence_refs=("sha256:aaa", "sha256:bbb", "sha256:aaa"),
    )
    assert findings[0].evidence_refs == ("sha256:aaa", "sha256:bbb")


def test_feature_risk_finding_validation_branches() -> None:
    with pytest.raises(
        ValidationError, match="non_null_count must equal row_count for an exact identifier"
    ):
        FeatureRiskFinding(
            finding_id="likely-identifier:row_id",
            kind="likely_identifier",
            column="row_id",
            message="x",
            row_count=3,
            non_null_count=2,
            distinct_count=3,
            uniqueness_rate=1.0,
            recommended_action="exclude_or_verify",
            evidence_refs=("sha256:aaa",),
        )
    with pytest.raises(
        ValidationError, match="distinct_count must equal row_count for an exact identifier"
    ):
        FeatureRiskFinding(
            finding_id="likely-identifier:row_id",
            kind="likely_identifier",
            column="row_id",
            message="x",
            row_count=3,
            non_null_count=3,
            distinct_count=2,
            uniqueness_rate=1.0,
            recommended_action="exclude_or_verify",
            evidence_refs=("sha256:aaa",),
        )
    with pytest.raises(
        ValidationError, match="uniqueness_rate must be 1.0 for an exact identifier"
    ):
        FeatureRiskFinding(
            finding_id="likely-identifier:row_id",
            kind="likely_identifier",
            column="row_id",
            message="x",
            row_count=3,
            non_null_count=3,
            distinct_count=3,
            uniqueness_rate=0.5,
            recommended_action="exclude_or_verify",
            evidence_refs=("sha256:aaa",),
        )
    with pytest.raises(ValidationError, match="evidence_refs must be unique within a finding"):
        FeatureRiskFinding(
            finding_id="likely-identifier:row_id",
            kind="likely_identifier",
            column="row_id",
            message="x",
            row_count=3,
            non_null_count=3,
            distinct_count=3,
            uniqueness_rate=1.0,
            recommended_action="exclude_or_verify",
            evidence_refs=("sha256:aaa", "sha256:aaa"),
        )
    with pytest.raises(ValidationError):
        FeatureRiskFinding(
            finding_id="likely-identifier:row_id",
            kind="likely_identifier",
            column="row_id",
            message="x",
            row_count=3,
            non_null_count=3,
            distinct_count=3,
            uniqueness_rate=1.0,
            recommended_action="exclude_or_verify",
            evidence_refs=(),
        )


def test_feature_risks_validation_branches() -> None:
    empty = FeatureRisks(
        current_snapshot_id="current",
        target_column="label",
        findings=(),
    )
    assert empty.findings == ()
    with pytest.raises(ValidationError, match="finding_id must be unique within feature risks"):
        FeatureRisks(
            current_snapshot_id="current",
            target_column="label",
            findings=(
                _exact_finding("row_id"),
                _exact_finding("other_id", finding_id="likely-identifier:row_id"),
            ),
        )
    with pytest.raises(
        ValidationError, match="finding columns must be unique within feature risks"
    ):
        FeatureRisks(
            current_snapshot_id="current",
            target_column="label",
            findings=(
                _exact_finding("row_id"),
                _exact_finding("row_id", finding_id="likely-identifier:row_id-again"),
            ),
        )
    with pytest.raises(
        ValidationError, match="feature-risk findings must not include the target column"
    ):
        FeatureRisks(
            current_snapshot_id="current",
            target_column="label",
            findings=(_exact_finding("label"),),
        )
