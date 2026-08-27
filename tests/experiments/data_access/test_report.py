from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dsx.experiments.data_access.evaluation import (
    ClaimAssessment,
    ClaimStatus,
    DecisionEvaluation,
    FactualClaim,
    PacketJsonPointerEvidence,
    SqlReplay,
    SqlReplayStatus,
)
from dsx.experiments.data_access.models import (
    CaseConfig,
    DatasetFormat,
    ModelConfig,
    OpaquePacket,
    PacketBuildMetrics,
    PricingSnapshot,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest
from dsx.experiments.data_access.report import (
    ArmMetrics,
    ClaimCounts,
    EvidenceMetrics,
    TokenUsage,
    _aggregate,
    _float,
    _int,
    _sequence,
    _value,
    build_experiment_report,
    estimated_cost_usd,
    summarize_arm,
    summarize_operational_attempts,
    token_usage_from_event,
)
from dsx.experiments.data_access.sql_tool import ReadOnlySqlTool


def _manifest(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = tmp_path / "case.csv"
    source.write_text("label,row_id\n0,a\n1,b\n", encoding="utf-8")
    return prepare_manifest(
        case=CaseConfig(
            case_id="case",
            task_prompt="task",
            dataset_path=str(source),
            dataset_format=DatasetFormat.csv,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"n": 2}),
        model=ModelConfig(model_identifier="test", system_prompt="system"),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1_000_000),
            cached_input=TokenPrice(usd_per_million_tokens=500_000),
            output=TokenPrice(usd_per_million_tokens=2_000_000),
            reasoning=TokenPrice(usd_per_million_tokens=3_000_000),
            source="test",
            effective_date="2026-08-26",
        ),
        packet_build_metrics=PacketBuildMetrics(estimated_cost_usd=10.0),
        database_path=tmp_path / "dataset.duckdb",
    )


def _evaluation() -> DecisionEvaluation:
    claim = FactualClaim(
        claim_id="n",
        statement="two",
        predicate="row_count",
        arguments={},
        asserted_value=2,
        evidence=(PacketJsonPointerEvidence(pointer="/n"),),
    )
    return DecisionEvaluation(
        assessments=(
            ClaimAssessment(
                claim=claim,
                status=ClaimStatus.unsupported,
                expected_value=2,
                evidence=(),
            ),
        )
    )


def test_token_normalization_and_arm_summary_with_costs(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    usage = token_usage_from_event(
        {
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens": 6,
                "output_tokens_details": {"reasoning_tokens": 1},
                "total_tokens": 16,
            }
        }
    )
    assert usage.cached_input_tokens == 2 and usage.reasoning_tokens == 1
    attempt = ReadOnlySqlTool(manifest.dataset.database_path).execute("SELECT * FROM dataset")
    packet = summarize_arm(
        {
            "arm": "dsx_packet",
            "repetition_id": "repetition-1",
            "repetition_number": 1,
            "elapsed_seconds": 4.0,
            "model_events": [{"elapsed_seconds": 1.5, "usage": usage}],
            "sql_attempts": (),
        },
        manifest=manifest,
        evaluation=_evaluation(),
    )
    full_data = summarize_arm(
        {
            "arm": "full_data",
            "repetition_id": "repetition-1",
            "repetition_number": 1,
            "elapsed_seconds": 5.0,
            "model_events": [{"elapsed_seconds": 2.0, "usage": usage}],
            "sql_attempts": (attempt,),
        },
        manifest=manifest,
        sql_replays=(
            SqlReplay(
                evidence_id="sql-test",
                status=SqlReplayStatus.matched,
                original_digest="0" * 64,
                replay_digest="0" * 64,
            ),
        ),
    )
    assert packet.inference_cost_usd == 22.0
    assert packet.amortized_dsx_costs[0].total_cost_usd == 32.0
    assert full_data.successful_discovery_attempts == 1
    combined = summarize_arm(
        {
            "arm": "packet_and_full_data",
            "repetition_id": "repetition-1",
            "repetition_number": 1,
            "elapsed_seconds": 6.0,
            "model_events": [{"elapsed_seconds": 2.5, "usage": usage}],
            "sql_attempts": (attempt,),
        },
        manifest=manifest,
    )
    assert combined.packet_build_cost_usd == 10.0
    assert combined.amortized_dsx_costs[0].total_cost_usd == 32.0
    report = build_experiment_report((packet, full_data, combined))
    assert report.report_version == "data-access-report-v2"
    for version in (None, "data-access-report-v1"):
        artifact = report.model_dump(mode="json")
        if version is None:
            del artifact["report_version"]
        else:
            artifact["report_version"] = version
        with pytest.raises(ValidationError, match="report_version"):
            type(report).model_validate(artifact)
    assert [(item.left_arm, item.right_arm) for item in report.arm_contrasts] == [
        ("dsx_packet", "full_data"),
        ("packet_and_full_data", "dsx_packet"),
        ("packet_and_full_data", "full_data"),
    ]
    assert report.arm_contrasts[0].elapsed_seconds == -1.0
    assert report.arm_contrasts[0].tool_calls == -1
    assert len(report.arm_aggregates) == 3
    aggregates = {aggregate.arm: aggregate for aggregate in report.arm_aggregates}
    assert aggregates["full_data"].packet_build_cost_usd_mean is None
    for arm in ("dsx_packet", "packet_and_full_data"):
        assert aggregates[arm].packet_build_cost_usd_mean == 10.0
        assert [cost.reuses for cost in aggregates[arm].amortized_dsx_costs_mean] == [1, 10, 100]


def _metric(arm: str, repetition_id: str, repetition_number: int) -> ArmMetrics:
    return ArmMetrics(
        arm=arm,
        repetition_id=repetition_id,
        repetition_number=repetition_number,
        elapsed_seconds=1.0,
        model_latency_seconds=0.5,
        tool_latency_seconds=0.5,
        model_calls=1,
        tool_calls=0,
        successful_discovery_attempts=0,
        failed_discovery_attempts=0,
        usage=TokenUsage(),
        inference_cost_usd=0.0,
        claims=ClaimCounts(),
        evidence=EvidenceMetrics(),
    )


def test_report_validation_structural_adapters_and_empty_aggregate_branches(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert _value({"x": 1}, "x") == 1
    assert _value(SimpleNamespace(x=2), "x") == 2
    assert _sequence({"one": None, "two": [1]}, "one", "two") == [1]
    assert _sequence({}, "none") == ()
    with pytest.raises(ValueError, match="sequence"):
        _sequence({"x": "bad"}, "x")
    assert _float({"x": None}, "x", 2.0) == 2.0 and _int({"x": None}, "x", 2) == 2
    for fn, input_value in ((_float, True), (_float, "x"), (_int, True), (_int, 1.0)):
        with pytest.raises(ValueError):
            fn({"x": input_value}, "x")
    assert token_usage_from_event({"input_tokens": 2, "output_tokens": 3}).total_tokens == 5
    direct = token_usage_from_event(
        {
            "usage": {
                "input_tokens": 5,
                "cached_input_tokens": 1,
                "output_tokens": 4,
                "reasoning_tokens": 1,
                "total_tokens": 9,
            }
        }
    )
    assert direct == TokenUsage(
        input_tokens=5,
        cached_input_tokens=1,
        output_tokens=4,
        reasoning_tokens=1,
        total_tokens=9,
    )
    for usage in (
        {"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0},
        {"input_tokens": 0, "output_tokens": 1, "reasoning_tokens": 2},
    ):
        with pytest.raises(ValueError):
            token_usage_from_event(usage)
    assert TokenUsage(input_tokens=1) + TokenUsage(output_tokens=2) == TokenUsage(
        input_tokens=1, output_tokens=2
    )
    assert estimated_cost_usd(direct, manifest.pricing) > 0
    empty = _aggregate(manifest.case and "full_data", ())
    assert empty.count == 0
    bad_ledger = {"arm": "full_data", "repetition_id": 1, "repetition_number": "one"}
    with pytest.raises(ValueError, match="repetition"):
        summarize_arm(bad_ledger, manifest=manifest)
    tool = ReadOnlySqlTool(manifest.dataset.database_path).execute("SELECT * FROM dataset")
    tool_ledger = SimpleNamespace(sql_attempt=tool, elapsed_seconds=0.2)
    model_response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=2, output_tokens=1, total_tokens=3)
    )
    object_ledger = SimpleNamespace(
        arm="full_data",
        repetition_id="objects",
        repetition_number=2,
        elapsed_seconds=1.0,
        model_calls=(SimpleNamespace(elapsed_seconds=0.3, response=model_response),),
        tool_calls=(tool_ledger,),
    )
    summary = summarize_arm(object_ledger, manifest=manifest)
    assert summary.usage.total_tokens == 3 and summary.tool_calls == 1
    direct_summary = summarize_arm(
        {
            "arm": "full_data",
            "repetition_id": "direct",
            "repetition_number": 3,
            "elapsed_seconds": 0.1,
            "usage": TokenUsage(input_tokens=1, total_tokens=1),
        },
        manifest=manifest,
    )
    assert direct_summary.usage.input_tokens == 1
    incomplete = build_experiment_report((_metric("dsx_packet", "only", 1),))
    assert incomplete.arm_contrasts == () and len(incomplete.arm_aggregates) == 3
    with pytest.raises(ValueError, match="duplicate"):
        build_experiment_report((_metric("full_data", "same", 1), _metric("full_data", "same", 1)))
    with pytest.raises(ValueError, match="repetition_number"):
        build_experiment_report((_metric("full_data", "bad", 1), _metric("dsx_packet", "bad", 2)))


def test_all_attempt_operational_metrics_include_retries_and_partial_attempts(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    tool = ReadOnlySqlTool(manifest.dataset.database_path)
    failed_sql = tool.execute("")
    succeeded_sql = tool.execute("SELECT * FROM dataset")
    retrying_repetition = SimpleNamespace(
        attempts=(
            SimpleNamespace(
                arms=(
                    SimpleNamespace(
                        arm="dsx_packet",
                        terminal_outcome="provider_error",
                        elapsed_seconds=1.0,
                        usage=TokenUsage(input_tokens=2, total_tokens=2),
                        model_calls=(SimpleNamespace(outcome="provider_error"),),
                        tool_calls=(SimpleNamespace(sql_attempt=failed_sql),),
                    ),
                )
            ),
            SimpleNamespace(
                arms=(
                    SimpleNamespace(
                        arm="dsx_packet",
                        terminal_outcome="completed",
                        elapsed_seconds=2.0,
                        usage=TokenUsage(output_tokens=3, total_tokens=3),
                        model_calls=(SimpleNamespace(outcome="completed"),),
                        tool_calls=(),
                    ),
                    SimpleNamespace(
                        arm="full_data",
                        terminal_outcome="completed",
                        elapsed_seconds=3.0,
                        usage=TokenUsage(input_tokens=4, total_tokens=4),
                        model_calls=(SimpleNamespace(outcome="transport_error"),),
                        tool_calls=(SimpleNamespace(sql_attempt=succeeded_sql),),
                    ),
                    SimpleNamespace(
                        arm="packet_and_full_data",
                        terminal_outcome="completed",
                        elapsed_seconds=5.0,
                        usage=TokenUsage(input_tokens=5, total_tokens=5),
                        model_calls=(SimpleNamespace(outcome="completed"),),
                        tool_calls=(SimpleNamespace(sql_attempt=succeeded_sql),),
                    ),
                )
            ),
        )
    )
    no_retry_repetition = SimpleNamespace(
        attempts=(
            SimpleNamespace(
                arms=(
                    SimpleNamespace(
                        arm="full_data",
                        terminal_outcome="completed",
                        elapsed_seconds=4.0,
                        model_calls=(
                            SimpleNamespace(
                                outcome="completed",
                                response=SimpleNamespace(
                                    usage=TokenUsage(input_tokens=1, total_tokens=1)
                                ),
                            ),
                        ),
                        tool_calls=(),
                    ),
                )
            ),
        )
    )
    operational = summarize_operational_attempts(
        (retrying_repetition, no_retry_repetition), manifest=manifest
    )
    packet, full_data, combined = operational.arm_summaries
    assert (
        packet.attempted_arm_runs,
        packet.completed_arm_runs,
        packet.abandoned_arm_runs,
        packet.provider_error_calls,
        packet.failed_sql_attempts,
        packet.usage.total_tokens,
    ) == (2, 1, 1, 1, 1, 5)
    assert (
        full_data.attempted_arm_runs,
        full_data.completed_arm_runs,
        full_data.abandoned_arm_runs,
        full_data.transport_error_calls,
        full_data.tool_calls,
    ) == (2, 2, 0, 1, 1)
    assert (
        combined.attempted_arm_runs,
        combined.completed_arm_runs,
        combined.abandoned_arm_runs,
        combined.tool_calls,
        combined.usage.total_tokens,
    ) == (1, 1, 0, 1, 5)
    report = build_experiment_report(
        (
            _metric("dsx_packet", "repetition", 1),
            _metric("full_data", "repetition", 1),
            _metric("packet_and_full_data", "repetition", 1),
        ),
        repetition_runs=(retrying_repetition, no_retry_repetition),
        manifest=manifest,
    )
    assert report.operational == operational
    with pytest.raises(ValueError, match="manifest"):
        build_experiment_report((), repetition_runs=(retrying_repetition,))
