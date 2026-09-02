from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from dsx.experiments.data_access import evaluation as evaluation_module
from dsx.experiments.data_access.evaluation import (
    ClaimArgument,
    ClaimBooleanValue,
    ClaimIntegerValue,
    ClaimListValue,
    ClaimNullValue,
    ClaimNumberValue,
    ClaimStatus,
    ClaimStringValue,
    DataAccessDecision,
    EvidenceResolution,
    EvidenceResolutionStatus,
    FactualClaim,
    Metric,
    OracleResult,
    PacketJsonPointerEvidence,
    SqlReplayStatus,
    SyntheticPilotOracle,
    ToolCallEvidence,
    V1PacketTraceAdapter,
    cited_tool_evidence_ids,
    evaluate_decision,
    provider_decision_schema,
    replay_sql_evidence,
)
from dsx.experiments.data_access.models import (
    Arm,
    CaseConfig,
    DatasetFormat,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    SqlAttempt,
    SqlAttemptOutcome,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest
from dsx.experiments.data_access.sql_tool import ReadOnlySqlTool


def _manifest(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = tmp_path / "case.json"
    source.write_text(
        json.dumps(
            {
                "rows": [
                    {"row_id": "a", "label": 0, "nullable": None},
                    {"row_id": "b", "label": 1, "nullable": 1},
                    {"row_id": "c", "label": 0, "nullable": 2},
                ]
            }
        ),
        encoding="utf-8",
    )
    return prepare_manifest(
        case=CaseConfig(
            case_id="pilot",
            task_prompt="make a plan",
            dataset_path=str(source),
            dataset_format=DatasetFormat.pilot_case_json,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"facts": {"row_count": 3}}),
        model=ModelConfig(model_identifier="test", system_prompt="test"),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=2),
            source="test",
            effective_date="2026-08-26",
        ),
        database_path=tmp_path / "dataset.duckdb",
    )


def _decision(*claims: FactualClaim) -> DataAccessDecision:
    return DataAccessDecision(
        primary_metric=Metric.recall_at_5_percent,
        supporting_metrics=(Metric.precision_at_5_percent,),
        review_budget_fraction=0.05,
        split_strategy="stratified",
        excluded_columns=("row_id",),
        reasoning="use the facts",
        limitations=("synthetic",),
        recommendation="rank cases",
        factual_claims=claims,
        narrative_claim_ids=tuple(claim.claim_id for claim in claims),
    )


def test_evaluates_supported_unsupported_contradicted_and_unverifiable_claims(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    decision = _decision(
        FactualClaim(
            claim_id="rows",
            statement="there are three rows",
            predicate="row_count",
            arguments={},
            asserted_value=3,
            evidence=(PacketJsonPointerEvidence(pointer="/facts/row_count"),),
        ),
        FactualClaim(
            claim_id="nulls",
            statement="nullable is missing one third",
            predicate="missingness",
            arguments={"column": "nullable"},
            asserted_value=1 / 3,
            evidence=(),
        ),
        FactualClaim(
            claim_id="wrong",
            statement="there are two positives",
            predicate="class_count",
            arguments={"label": 1},
            asserted_value=2,
            evidence=(PacketJsonPointerEvidence(pointer="/facts/row_count"),),
        ),
        FactualClaim(
            claim_id="unknown",
            statement="not a registered fact",
            predicate="future_predicate",
            arguments={},
            asserted_value=True,
            evidence=(),
        ),
        FactualClaim(
            claim_id="bad-evidence",
            statement="three rows still",
            predicate="row_count",
            arguments={},
            asserted_value=3,
            evidence=(PacketJsonPointerEvidence(pointer="/missing"),),
        ),
    )
    evaluation = evaluate_decision(decision, manifest, arm=Arm.dsx_packet)
    assert [assessment.status for assessment in evaluation.assessments] == [
        ClaimStatus.supported,
        ClaimStatus.unsupported,
        ClaimStatus.contradicted,
        ClaimStatus.unverifiable,
        ClaimStatus.unsupported,
    ]
    assert evaluation.invalid_evidence_references == 1
    assert evaluation.counts == {
        "supported": 1,
        "unsupported": 2,
        "contradicted": 1,
        "unverifiable": 1,
    }


def test_resolves_tool_evidence_and_replays_cited_successful_sql(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    attempt = ReadOnlySqlTool(manifest.dataset.database_path).execute(
        "SELECT count(*) AS row_count FROM dataset"
    )
    assert attempt.result is not None
    decision = _decision(
        FactualClaim(
            claim_id="rows",
            statement="there are three rows",
            predicate="row_count",
            arguments={},
            asserted_value=3,
            evidence=(ToolCallEvidence(evidence_id=attempt.result.evidence_id),),
        )
    )
    evaluation = evaluate_decision(decision, manifest, (attempt,), arm=Arm.full_data)
    assert evaluation.assessments[0].status is ClaimStatus.supported
    evidence_ids = cited_tool_evidence_ids(decision)
    assert evidence_ids == (attempt.result.evidence_id,)
    replay = replay_sql_evidence(manifest, (attempt,), evidence_ids)
    assert replay[0].status.value == "matched"


def test_evidence_kinds_are_limited_to_each_experiment_arm(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    attempt = ReadOnlySqlTool(manifest.dataset.database_path).execute(
        "SELECT count(*) AS row_count FROM dataset"
    )
    assert attempt.result is not None
    packet_claim = FactualClaim(
        claim_id="packet-rows",
        statement="there are three rows",
        predicate="row_count",
        arguments={},
        asserted_value=3,
        evidence=(PacketJsonPointerEvidence(pointer="/facts/row_count"),),
    )
    tool_claim = FactualClaim(
        claim_id="tool-rows",
        statement="there are three rows",
        predicate="row_count",
        arguments={},
        asserted_value=3,
        evidence=(ToolCallEvidence(evidence_id=attempt.result.evidence_id),),
    )
    packet_in_data = evaluate_decision(
        _decision(packet_claim), manifest, arm=Arm.full_data
    )
    tool_in_packet = evaluate_decision(
        _decision(tool_claim), manifest, (attempt,), arm=Arm.dsx_packet
    )
    combined = evaluate_decision(
        _decision(packet_claim, tool_claim),
        manifest,
        (attempt,),
        arm=Arm.packet_and_full_data,
    )
    assert packet_in_data.invalid_evidence_references == 1
    assert tool_in_packet.invalid_evidence_references == 1
    assert [assessment.status for assessment in combined.assessments] == [
        ClaimStatus.supported,
        ClaimStatus.supported,
    ]


def test_predicate_specific_sql_evidence_rejects_unrelated_values_and_accepts_shipped_facts(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    tool = ReadOnlySqlTool(manifest.dataset.database_path)
    valid = (
        (_claim("row_count", 3), "SELECT count(*) AS row_count FROM dataset"),
        (
            _claim("class_count", 1, {"label": 1}),
            "SELECT label, count(*) AS class_count FROM dataset WHERE label = 1 GROUP BY label",
        ),
        (
            _claim("class_rate", 1 / 3, {"label": 1}),
            "SELECT label, count(*) * 1.0 / (SELECT count(*) FROM dataset) AS class_rate "
            "FROM dataset WHERE label = 1 GROUP BY label",
        ),
        (
            _claim("majority_baseline", 2 / 3),
            "SELECT max(n) * 1.0 / (SELECT count(*) FROM dataset) AS majority_baseline "
            "FROM (SELECT count(*) AS n FROM dataset GROUP BY label)",
        ),
        (
            _claim("review_count", 2, {"fraction": 0.5}),
            "SELECT round(count(*) * 0.5) AS review_count FROM dataset",
        ),
        (
            _claim("missingness", 1 / 3, {"column": "nullable"}),
            "SELECT 'nullable' AS column, count(*) * 1.0 / (SELECT count(*) FROM dataset) "
            "AS missingness FROM dataset WHERE nullable IS NULL",
        ),
        (
            _claim("uniqueness", 1.0, {"column": "row_id"}),
            "SELECT 'row_id' AS column, count(DISTINCT row_id) * 1.0 / count(*) AS uniqueness "
            "FROM dataset",
        ),
        (
            _claim("likely_id", True, {"column": "row_id"}),
            "SELECT 'row_id' AS column, count(DISTINCT row_id) = count(*) AS likely_id "
            "FROM dataset",
        ),
        (
            _claim("recommended_exclusions", ("row_id",)),
            "SELECT 'row_id' AS excluded_column FROM dataset "
            "HAVING count(DISTINCT row_id) = count(*)",
        ),
    )
    attempts = tuple(tool.execute(sql) for _claim_value, sql in valid)
    assert all(attempt.result is not None for attempt in attempts)
    claims = tuple(
        FactualClaim(
            claim_id=f"valid-{index}",
            statement=claim.statement,
            predicate=claim.predicate,
            arguments=claim.argument_values,
            asserted_value=claim.asserted_native_value,
            evidence=(ToolCallEvidence(evidence_id=attempt.result.evidence_id),),  # type: ignore[union-attr]
        )
        for index, (claim, _sql), attempt in zip(range(len(valid)), valid, attempts, strict=True)
    )
    evaluation = evaluate_decision(_decision(*claims), manifest, attempts, arm=Arm.full_data)
    assert [assessment.status for assessment in evaluation.assessments] == [
        ClaimStatus.supported,
    ] * len(claims)

    unrelated = tool.execute("SELECT 2 AS row_count FROM dataset LIMIT 1")
    wrong_alias = tool.execute("SELECT count(*) AS unrelated FROM dataset")
    assert unrelated.result is not None and wrong_alias.result is not None
    unsupported_claims = (
        FactualClaim(
            claim_id="unrelated-sql",
            statement="three rows",
            predicate="row_count",
            arguments={},
            asserted_value=3,
            evidence=(ToolCallEvidence(evidence_id=unrelated.result.evidence_id),),
        ),
        FactualClaim(
            claim_id="wrong-shape",
            statement="three rows",
            predicate="row_count",
            arguments={},
            asserted_value=3,
            evidence=(ToolCallEvidence(evidence_id=wrong_alias.result.evidence_id),),
        ),
        FactualClaim(
            claim_id="unrelated-packet",
            statement="three rows",
            predicate="row_count",
            arguments={},
            asserted_value=3,
            evidence=(PacketJsonPointerEvidence(pointer=""),),
        ),
    )
    unsupported = evaluate_decision(
        _decision(*unsupported_claims),
        manifest,
        (unrelated, wrong_alias),
        arm=Arm.packet_and_full_data,
    )
    assert all(
        assessment.status is ClaimStatus.unsupported for assessment in unsupported.assessments
    )
    assert all(
        resolution.status is EvidenceResolutionStatus.resolved and not resolution.supports_claim
        for assessment in unsupported.assessments
        for resolution in assessment.evidence
    )


def test_provider_schema_is_strict_closed_and_uses_only_explicit_value_variants() -> None:
    provider = provider_decision_schema("data-access")
    assert provider["type"] == "json_schema" and provider["strict"] is True
    schema = provider["schema"]

    def assert_closed(node: object) -> None:
        if isinstance(node, dict):
            assert "default" not in node
            assert "oneOf" not in node
            assert "discriminator" not in node
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", ())) == set(node.get("properties", {}))
            for child in node.values():
                assert_closed(child)
        elif isinstance(node, list):
            for child in node:
                assert_closed(child)

    assert_closed(schema)
    claim = schema["$defs"]["FactualClaim"]
    assert claim["properties"]["arguments"]["type"] == "array"
    assert "additionalProperties" not in claim["properties"]["arguments"]
    value_schema = schema["$defs"]["ClaimValue"]
    assert {entry["$ref"].rsplit("/", maxsplit=1)[-1] for entry in value_schema["anyOf"]} == {
        "ClaimStringValue",
        "ClaimIntegerValue",
        "ClaimNumberValue",
        "ClaimBooleanValue",
        "ClaimNullValue",
        "ClaimListValue",
    }


def test_provider_payload_requires_explicit_values_and_rejects_extra_properties() -> None:
    payload = {
        "primary_metric": "recall_at_5_percent",
        "supporting_metrics": ["precision_at_5_percent"],
        "review_budget_fraction": 0.05,
        "split_strategy": "stratified",
        "excluded_columns": ["row_id"],
        "reasoning": "evidence based",
        "limitations": ["synthetic"],
        "recommendation": "rank",
        "factual_claims": [
            {
                "claim_id": "rows",
                "statement": "three rows",
                "predicate": "row_count",
                "arguments": [],
                "asserted_value": {"kind": "integer", "value": 3},
                "evidence": [{"kind": "packet_json_pointer", "pointer": "/facts/rows"}],
            }
        ],
        "narrative_claim_ids": ["rows"],
    }
    decision = DataAccessDecision.model_validate(payload)
    assert decision.factual_claims[0].asserted_native_value == 3
    payload["factual_claims"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        DataAccessDecision.model_validate(payload)


def _claim(
    predicate: str,
    asserted_value: Any,
    arguments: dict[str, Any] | None = None,
    evidence: tuple[PacketJsonPointerEvidence | ToolCallEvidence, ...] = (),
) -> FactualClaim:
    return FactualClaim(
        claim_id=f"claim-{predicate}",
        statement=predicate,
        predicate=predicate,
        arguments=arguments or {},
        asserted_value=asserted_value,
        evidence=evidence,
    )


def test_evaluation_contract_helpers_and_all_pilot_oracle_predicates(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    connection = duckdb.connect(manifest.dataset.database_path, read_only=True)
    oracle = SyntheticPilotOracle(target_column="label")
    try:
        assert oracle.resolve(connection, _claim("row_count", 3)).value == 3
        assert oracle.resolve(connection, _claim("class_count", 1, {"label": 1})).value == 1
        assert oracle.resolve(connection, _claim("class_rate", 1 / 3, {"label": 1})).value == 1 / 3
        assert oracle.resolve(connection, _claim("majority_baseline", 2 / 3)).value == 2 / 3
        assert oracle.resolve(connection, _claim("review_count", 2, {"fraction": 0.5})).value == 2
        assert (
            oracle.resolve(connection, _claim("missingness", 1 / 3, {"column": "nullable"})).value
            == 1 / 3
        )
        assert (
            oracle.resolve(connection, _claim("uniqueness", 1.0, {"column": "row_id"})).value == 1.0
        )
        assert (
            oracle.resolve(connection, _claim("likely_id", True, {"column": "row_id"})).value
            is True
        )
        assert (
            oracle.resolve(connection, _claim("likely_id", False, {"column": "label"})).value
            is False
        )
        assert oracle.resolve(connection, _claim("recommended_exclusions", ("row_id",))).value == (
            "row_id",
        )
        assert oracle.resolve(connection, _claim("future", True)).known is False
        for claim in (
            _claim("class_count", 1),
            _claim("review_count", 0, {"fraction": True}),
            _claim("review_count", 0, {"fraction": 2.0}),
            _claim("missingness", 0, {"column": "nope"}),
        ):
            with pytest.raises(ValueError):
                oracle.resolve(connection, claim)
    finally:
        connection.close()
    unknown_version = manifest.model_copy(
        update={"case": manifest.case.model_copy(update={"oracle_version": "v2"})}
    )
    with pytest.raises(ValueError, match="registered"):
        evaluation_module.oracle_for_manifest(unknown_version)
    assert evaluation_module._quote_identifier('a"b') == '"a""b"'
    assert evaluation_module._number(1) == 1.0
    with pytest.raises(ValueError):
        evaluation_module._number(False)


def test_claim_values_evidence_and_low_level_evaluation_branches(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    values = (
        ClaimStringValue(kind="string", value="s"),
        ClaimIntegerValue(kind="integer", value=1),
        ClaimNumberValue(kind="number", value=1.5),
        ClaimBooleanValue(kind="boolean", value=True),
        ClaimNullValue(kind="null", value=None),
        ClaimListValue(
            kind="list",
            items=(ClaimStringValue(kind="string", value="x"),),
        ),
    )
    assert [value.native for value in values] == ["s", 1, 1.5, True, None, ("x",)]
    original = ClaimStringValue(kind="string", value="already")
    assert evaluation_module._claim_value_payload(original) is original
    assert evaluation_module._claim_value_payload({"kind": "integer", "value": 3})["value"] == 3
    assert evaluation_module._claim_value_payload(None)["kind"] == "null"
    assert evaluation_module._claim_value_payload(("a", 1))["kind"] == "list"
    with pytest.raises(ValueError, match="scalar"):
        evaluation_module._claim_value_payload([["nested"]])
    with pytest.raises(ValueError, match="scalar"):
        evaluation_module._claim_value_payload({"not": "a tagged value"})
    assert ToolCallEvidence.model_validate({"evidence_id": "sql-a"}).kind.value == "tool_call"
    assert ToolCallEvidence.adapt_local_reference("raw") == "raw"
    assert (
        PacketJsonPointerEvidence.model_validate({"pointer": "/a"}).kind.value
        == "packet_json_pointer"
    )
    duplicate = FactualClaim.model_construct(
        claim_id="dupe",
        statement="dupe",
        predicate="row_count",
        arguments=(
            ClaimArgument(key="a", value=ClaimIntegerValue(kind="integer", value=1)),
            ClaimArgument(key="a", value=ClaimIntegerValue(kind="integer", value=2)),
        ),
        asserted_value=ClaimIntegerValue(kind="integer", value=3),
        evidence=(),
    )
    with pytest.raises(ValueError, match="unique"):
        duplicate.validate_argument_keys()
    assert FactualClaim.adapt_local_value_inputs("raw") == "raw"
    assert FactualClaim.adapt_local_value_inputs({"arguments": {}}) == {"arguments": []}
    decision = _decision(_claim("row_count", 3))
    assert decision.excludes_row_id
    with pytest.raises(ValidationError, match="unique"):
        DataAccessDecision.model_validate(
            {
                **decision.model_dump(mode="python"),
                "factual_claims": (decision.factual_claims[0],) * 2,
            }
        )
    with pytest.raises(ValidationError, match="narrative"):
        DataAccessDecision.model_validate(
            {**decision.model_dump(mode="python"), "narrative_claim_ids": ("missing",)}
        )
    for bad in ("", "not valid!", "-"):
        with pytest.raises(ValueError):
            provider_decision_schema(bad)
    assert evaluation_module._responses_schema([{"oneOf": [1], "discriminator": {}}]) == [
        {"anyOf": [1]}
    ]
    pointer = PacketJsonPointerEvidence(pointer="/facts/row_count")
    resolved = evaluation_module._resolve_evidence(
        pointer,
        packet=manifest.packet,
        sql_results={},
        claim=_claim("row_count", 3),
        target_column="label",
        packet_trace_adapter=V1PacketTraceAdapter(),
    )
    assert resolved.status is EvidenceResolutionStatus.resolved and resolved.supports_claim
    invalid = evaluation_module._resolve_evidence(
        ToolCallEvidence(evidence_id="missing"),
        packet=manifest.packet,
        sql_results={},
        claim=_claim("row_count", 3),
        target_column="label",
        packet_trace_adapter=V1PacketTraceAdapter(),
    )
    assert invalid.status is EvidenceResolutionStatus.invalid
    with pytest.raises(ValidationError, match="requires"):
        EvidenceResolution(
            reference=pointer,
            status=EvidenceResolutionStatus.resolved,
            supports_claim=False,
        )
    with pytest.raises(ValidationError, match="requires"):
        EvidenceResolution(reference=pointer, status=EvidenceResolutionStatus.invalid)
    assert evaluation_module._values_equal(True, True)
    assert not evaluation_module._values_equal(True, 1)
    assert evaluation_module._dataset_query("SELECT count(*) FROM dataset", "count(")
    assert not evaluation_module._sql_value_equal("not-a-number", 1)
    failed_attempt = SqlAttempt(
        attempt_id="failed",
        sql="SELECT count(*) FROM dataset",
        outcome=SqlAttemptOutcome.execution_error,
        elapsed_seconds=0.0,
        error_message="failed",
    )
    assert not evaluation_module._sql_supports_claim(
        failed_attempt, _claim("row_count", 3), target_column="label"
    )
    valid_attempt = ReadOnlySqlTool(manifest.dataset.database_path).execute(
        "SELECT count(*) AS row_count FROM dataset"
    )
    assert not evaluation_module._sql_supports_claim(
        valid_attempt, _claim("not_registered", 3), target_column="label"
    )
    assert V1PacketTraceAdapter().replay_computation({}, "/") == "not_applicable"
    assert evaluation_module.sql_attempts_from_ledger(()) == ()
    with pytest.raises(ValueError, match="SqlAttempt"):
        evaluation_module.sql_attempts_from_ledger((object(),))
    with pytest.raises(ValidationError):
        OracleResult(known=True, value=object())
    assert OracleResult(known=False, value=None).value is None


def test_unverifiable_oracle_and_sql_replay_mismatch_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    decision = _decision(_claim("row_count", 3))

    class RaisingOracle:
        def resolve(
            self, connection: duckdb.DuckDBPyConnection, claim: FactualClaim
        ) -> OracleResult:
            del connection, claim
            raise ValueError("bad predicate")

    assessment = evaluate_decision(
        decision, manifest, arm=Arm.dsx_packet, oracle=RaisingOracle()
    ).assessments[0]
    assert assessment.status is ClaimStatus.unverifiable
    attempt = ReadOnlySqlTool(manifest.dataset.database_path).execute(
        "SELECT count(*) AS row_count FROM dataset"
    )
    assert attempt.result is not None
    connection = duckdb.connect(manifest.dataset.database_path)
    connection.execute("INSERT INTO dataset VALUES ('d', 0, NULL)")
    connection.close()
    replay = replay_sql_evidence(manifest, (attempt,), (attempt.result.evidence_id,))
    assert replay[0].status is SqlReplayStatus.mismatched
    connection = duckdb.connect(manifest.dataset.database_path)
    connection.execute("DELETE FROM dataset WHERE row_id = 'd'")
    connection.close()

    class FailedTool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def execute(self, sql: object) -> Any:
            del sql
            return ReadOnlySqlTool(manifest.dataset.database_path).execute("SELECT * FROM absent")

    monkeypatch.setattr(evaluation_module, "ReadOnlySqlTool", FailedTool)
    failed = replay_sql_evidence(manifest, (attempt,), (attempt.result.evidence_id,))
    assert failed[0].status is SqlReplayStatus.replay_failed
    assert replay_sql_evidence(manifest, (attempt,), ("unknown",)) == ()
