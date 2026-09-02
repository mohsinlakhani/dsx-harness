"""Tests for Context Lift's persisted contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from dsx.experiments.context_lift.models import (
    AnalysisDecision,
    Arm,
    ArmOutcome,
    ClassCount,
    ClassRate,
    ColumnFact,
    DatasetShape,
    Metric,
    ModelRequest,
    OutcomeKind,
    Packet,
    PilotCase,
    PilotRow,
    RequestContext,
    RunManifest,
)


def make_packet() -> Packet:
    return Packet(
        version="1",
        dataset_shape=DatasetShape(rows=20, columns=6),
        class_counts=(ClassCount(label=0, count=11), ClassCount(label=1, count=9)),
        class_rates=(ClassRate(label=0, rate=0.55), ClassRate(label=1, rate=0.45)),
        majority_baseline_accuracy=0.55,
        column_facts=(
            ColumnFact(name="row_id", likely_id=True, missingness=0.0),
            ColumnFact(name="signal_a", likely_id=False, missingness=0.1),
        ),
        metric_guidance="Use balanced accuracy.",
        split_guidance="Use a stratified holdout.",
        exclusions=("row_id",),
        limitations=("Small sample",),
        evidence_references=("E1",),
    )


def make_decision() -> AnalysisDecision:
    return AnalysisDecision(
        primary_metric=Metric.recall_at_5_percent,
        supporting_metrics=(Metric.precision_at_5_percent, Metric.pr_auc),
        review_budget_fraction=0.05,
        split_strategy="stratified holdout",
        excluded_columns=("row_id",),
        reasoning="The classes are slightly imbalanced.",
        limitations=("Small sample",),
        recommendation="Proceed with a baseline model.",
        packet_citations=("E1",),
    )


def test_pilot_case_is_frozen_and_serializes_rows_as_json() -> None:
    """Removing frozen value semantics would allow a case to change after generation."""
    case = PilotCase(
        generation_seed=7,
        task_text="Classify the records.",
        rows=(
            PilotRow(
                row_id="r-1",
                label=1,
                signal_a=1.5,
                signal_b=2.5,
                noise=0.2,
                category="north",
                nullable_numeric=None,
            ),
        ),
    )

    assert case.manual_review_fraction == 0.05
    assert json.loads(case.model_dump_json())["rows"][0]["row_id"] == "r-1"
    with pytest.raises(ValidationError):
        case.generation_seed = 8  # type: ignore[misc]


def test_contracts_reject_unknown_persisted_fields() -> None:
    """Removing extra-field rejection would silently accept persisted contract drift."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PilotRow.model_validate(
            {
                "row_id": "r-1",
                "label": 0,
                "signal_a": 0.0,
                "signal_b": 0.0,
                "noise": 0.0,
                "category": "south",
                "nullable_numeric": 4.0,
                "unexpected": True,
            }
        )


def test_packet_round_trips_with_tuple_facts_and_evidence() -> None:
    """Changing packet fields or tuple contracts would lose reproducibility evidence."""
    packet = make_packet()

    restored = Packet.model_validate_json(packet.model_dump_json())

    assert restored == packet
    assert restored.column_facts[0].likely_id is True
    assert restored.evidence_references == ("E1",)


def test_packet_class_distributions_are_deeply_immutable_and_json_serializable() -> None:
    """Replacing tuple value objects with dicts would permit evidence mutation."""
    packet = make_packet()

    assert json.loads(packet.model_dump_json())["class_counts"] == [
        {"label": 0, "count": 11},
        {"label": 1, "count": 9},
    ]
    with pytest.raises(ValidationError):
        packet.class_counts[0].count = 12  # type: ignore[misc]
    with pytest.raises(ValidationError):
        packet.class_counts += (ClassCount(label=0, count=12),)  # type: ignore[misc]


def test_packet_class_distribution_values_are_bounded() -> None:
    """Removing numeric bounds would allow impossible class distributions."""
    with pytest.raises(ValidationError):
        ClassCount(label=0, count=-1)
    with pytest.raises(ValidationError):
        ClassRate(label=1, rate=1.01)


def test_decision_uses_typed_metrics_and_exact_review_budget() -> None:
    """Replacing typed values with strings would accept unscorable metric claims."""
    decision = make_decision()

    assert decision.primary_metric is Metric.recall_at_5_percent
    assert decision.supporting_metrics == (Metric.precision_at_5_percent, Metric.pr_auc)
    assert decision.review_budget_fraction == 0.05
    with pytest.raises(ValidationError):
        AnalysisDecision.model_validate(
            {**decision.model_dump(), "primary_metric": "balanced_accuracy"}
        )
    with pytest.raises(ValidationError):
        AnalysisDecision.model_validate({**decision.model_dump(), "review_budget_fraction": 0.1})


def test_decision_keeps_wrong_metrics_and_row_id_omission_observable() -> None:
    """Over-validating model output would hide analytically wrong decisions from scoring."""
    decision = AnalysisDecision(
        primary_metric=Metric.accuracy,
        supporting_metrics=(Metric.pr_auc,),
        review_budget_fraction=0.05,
        split_strategy="random split",
        excluded_columns=("signal_a",),
        reasoning="Accuracy is simple.",
        limitations=(),
        recommendation="Proceed.",
        packet_citations=(),
    )

    assert decision.primary_metric is Metric.accuracy
    assert decision.excludes_row_id is False
    assert make_decision().excludes_row_id is True


def test_model_request_has_only_profile_packet_as_treatment_context() -> None:
    """Adding another treatment field would break the controlled arm comparison."""
    request = ModelRequest(
        model_identifier="gpt-test",
        system_prompt="System",
        task_prompt="Task",
        response_schema_name="analysis_decision",
        context=RequestContext(profile_packet=None),
    )

    assert request.context.profile_packet is None
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RequestContext.model_validate({"profile_packet": make_packet(), "hint": "extra"})


def test_completed_outcome_requires_a_decision_and_errors_cannot_include_one() -> None:
    """Removing outcome consistency would make result aggregation ambiguous."""
    completed = ArmOutcome(
        arm=Arm.packet_on,
        outcome_kind=OutcomeKind.completed,
        request_digest="request",
        common_projection_digest="common",
        attempt_number=1,
        request_number=2,
        parsed_decision=make_decision(),
        raw_response='{"ok": true}',
    )
    assert completed.parsed_decision is not None

    with pytest.raises(ValidationError, match="parsed_decision"):
        ArmOutcome(
            arm=Arm.packet_off,
            outcome_kind=OutcomeKind.completed,
            request_digest="request",
            common_projection_digest="common",
            attempt_number=1,
            request_number=2,
        )
    with pytest.raises(ValidationError, match="must not include parsed_decision"):
        ArmOutcome(
            arm=Arm.packet_off,
            outcome_kind=OutcomeKind.transport_error,
            request_digest="request",
            common_projection_digest="common",
            attempt_number=1,
            request_number=2,
            parsed_decision=make_decision(),
        )


def test_run_manifest_freezes_three_pair_intent_and_request_commitments() -> None:
    """Changing run intent after publication would invalidate the experiment ledger."""
    manifest = RunManifest(
        case_digest="a" * 64,
        packet_version="pilot-v1",
        model_identifier="gpt-test",
        generation_seed=20260819,
        base_order_seed=17,
        pair_ids=("one", "two", "three"),
        pair_order_seeds=(11, 12, 13),
        packet_off_request_digest="b" * 64,
        packet_on_request_digest="c" * 64,
        common_projection_digest="d" * 64,
    )

    assert manifest.intended_pair_count == 3
    assert manifest.claim_label == "one-case unscored information-availability pilot"
    assert RunManifest.model_validate_json(manifest.model_dump_json()) == manifest
    with pytest.raises(ValidationError):
        RunManifest.model_validate(
            {**manifest.model_dump(), "pair_ids": ("one", "two")}
        )
