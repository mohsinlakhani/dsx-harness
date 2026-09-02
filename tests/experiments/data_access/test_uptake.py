from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dsx.builders.build import build_packet
from dsx.builders.models import PacketBuildRequest
from dsx.experiments.data_access.canonical import canonical_digest, canonical_json
from dsx.experiments.data_access.cli import RUN_MANIFEST_FILENAME, DataAccessRunManifest
from dsx.experiments.data_access.evaluation import DataAccessDecision, Metric
from dsx.experiments.data_access.execution import (
    ResponsesEnvelope,
    ScriptedResponsesClient,
    Usage,
    run_experiment,
)
from dsx.experiments.data_access.models import (
    Arm,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest
from dsx.experiments.data_access.realistic import freeze_case
from dsx.packet import DsxPacket
from tests.builders.helpers import write_csv


def _eligible_rows() -> list[dict[str, object]]:
    # Constant `noise` so the only uniqueness-1.0 identifier is `row_id`.
    # Task 1's `noise: index` is also unique and would appear as a second identifier.
    return [
        {
            "row_id": f"r{index}",
            "label": 1 if index == 0 else 0,
            "nullable": None if index < 2 else index,
            "noise": 0,
        }
        for index in range(20)
    ]


def _eligible_packet(tmp_path) -> DsxPacket:
    return build_packet(
        PacketBuildRequest(
            dataset_path=write_csv(tmp_path / "data.csv", _eligible_rows()),
            target_column="label",
            packet_id="eligible-v1",
        )
    ).packet


def _module_index(packet: DsxPacket, module_id: str) -> int:
    for index, module in enumerate(packet.modules):
        if module.module_id == module_id:
            return index
    raise AssertionError(f"missing module {module_id}")


def _rebuild_packet(packet: DsxPacket, module_id: str, content: Any) -> DsxPacket:
    payload = json.loads(packet.canonical_json())
    for module in payload["modules"]:
        if module["module_id"] == module_id:
            module["content"] = content
            return DsxPacket.model_validate(payload)
    raise AssertionError(f"missing module {module_id}")


def _decision(packet: DsxPacket | None = None, **overrides: object) -> DataAccessDecision:
    pointer = "/modules/4/content"
    if packet is not None:
        pointer = f"/modules/{_module_index(packet, 'feature-risks')}/content"
    payload: dict[str, object] = {
        "primary_metric": Metric.recall_at_5_percent,
        "supporting_metrics": (),
        "review_budget_fraction": 0.05,
        "split_strategy": "stratified",
        "excluded_columns": ("row_id",),
        "reasoning": "Exclude the identifier because of class imbalance.",
        "limitations": ("synthetic",),
        "recommendation": "rank for review",
        "factual_claims": (
            {
                "claim_id": "c1",
                "statement": "row_id is unique",
                "predicate": "likely_id",
                "arguments": {"column": "row_id"},
                "asserted_value": True,
                "evidence": ({"kind": "packet_json_pointer", "pointer": pointer},),
            },
        ),
        "narrative_claim_ids": ("c1",),
    }
    payload.update(overrides)
    return DataAccessDecision.model_validate(payload)


def test_packet_arm_uses_feature_risk_findings(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake

    packet = _eligible_packet(tmp_path)
    record = evaluate_uptake(
        _decision(packet),
        packet,
        arm=Arm.dsx_packet,
        repetition_id="repetition-001",
        target_column="label",
    )
    assert record.identifiers.columns == ("row_id",)
    assert record.identifiers.applicable is True
    assert record.identifiers.all_excluded is True
    assert record.imbalance.applicable is True
    assert record.imbalance.acknowledged is True
    assert record.packet_module_citations is not None
    assert record.packet_module_citations.feature_risks is True


def test_full_data_arm_has_no_packet_citations_and_misses_exclusions(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake

    packet = _eligible_packet(tmp_path)
    record = evaluate_uptake(
        _decision(
            packet,
            excluded_columns=(),
            reasoning="no issues",
            factual_claims=(),
            narrative_claim_ids=(),
        ),
        packet,
        arm=Arm.full_data,
        repetition_id="repetition-001",
        target_column="label",
    )
    assert record.identifiers.missed == ("row_id",)
    assert record.imbalance.acknowledged is False
    assert record.packet_module_citations is None


def test_identifier_columns_for_arm_uses_feature_risks_on_packet_arms(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import identifier_columns_for_arm

    packet = _eligible_packet(tmp_path)
    expected = ("row_id",)
    assert identifier_columns_for_arm(packet, arm=Arm.dsx_packet, target_column="label") == expected
    assert (
        identifier_columns_for_arm(packet, arm=Arm.packet_and_full_data, target_column="label")
        == expected
    )


def test_identifier_columns_for_arm_uses_column_profile_uniqueness_for_full_data(
    tmp_path,
) -> None:
    from dsx.experiments.data_access.uptake import identifier_columns_for_arm

    packet = _eligible_packet(tmp_path)
    assert identifier_columns_for_arm(packet, arm=Arm.full_data, target_column="label") == (
        "row_id",
    )


def test_packet_arm_without_identifier_columns_is_not_applicable(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake, identifier_columns_for_arm

    rows = [
        {"label": 0, "group": "a", "score": 1},
        {"label": 0, "group": "a", "score": 1},
        {"label": 1, "group": "b", "score": 2},
        {"label": 1, "group": "b", "score": 2},
    ]
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=write_csv(tmp_path / "plain.csv", rows),
            target_column="label",
            packet_id="plain-v1",
        )
    ).packet
    assert identifier_columns_for_arm(packet, arm=Arm.dsx_packet, target_column="label") == ()
    assert identifier_columns_for_arm(packet, arm=Arm.full_data, target_column="label") == ()
    record = evaluate_uptake(
        _decision(packet, excluded_columns=()),
        packet,
        arm=Arm.dsx_packet,
        repetition_id="repetition-001",
        target_column="label",
    )
    assert record.identifiers.columns == ()
    assert record.identifiers.applicable is False
    assert record.identifiers.all_excluded is None
    assert record.packet_module_citations is not None


def test_module_id_for_pointer_indexes_modules_and_rejects_invalid(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import module_id_for_pointer

    packet = _eligible_packet(tmp_path)
    feature_index = _module_index(packet, "feature-risks")
    assert module_id_for_pointer(packet, f"/modules/{feature_index}/content") == "feature-risks"
    assert module_id_for_pointer(packet, f"/modules/{feature_index}") == "feature-risks"
    assert module_id_for_pointer(packet, "/modules/not-an-index/content") is None
    assert module_id_for_pointer(packet, "/modules/99/content") is None
    assert module_id_for_pointer(packet, "/facts/row_count") is None
    assert module_id_for_pointer(packet, f"/modules/{feature_index}content") is None


def test_uptake_record_models_are_frozen_contracts() -> None:
    from dsx.experiments.data_access.uptake import (
        IdentifierUptake,
        ImbalanceUptake,
        PacketModuleCitations,
        UptakeRecord,
    )

    identifiers = IdentifierUptake(
        columns=("row_id",),
        excluded=("row_id",),
        missed=(),
        applicable=True,
        all_excluded=True,
    )
    imbalance = ImbalanceUptake(applicable=True, acknowledged=True)
    citations = PacketModuleCitations(
        column_profile=False,
        feature_risks=True,
        data_traps=False,
    )
    record = UptakeRecord(
        arm=Arm.dsx_packet,
        repetition_id="repetition-001",
        identifiers=identifiers,
        imbalance=imbalance,
        packet_module_citations=citations,
    )
    assert record.identifiers.all_excluded is True
    assert record.identifiers.applicable is True
    assert record.imbalance.acknowledged is True
    none_imbalance = ImbalanceUptake(applicable=False, acknowledged=None)
    assert none_imbalance.acknowledged is None
    empty = IdentifierUptake(
        columns=(),
        excluded=(),
        missed=(),
        applicable=False,
        all_excluded=None,
    )
    assert empty.applicable is False
    assert empty.all_excluded is None
    with pytest.raises(ValidationError):
        IdentifierUptake(
            columns=("row_id",),
            excluded=("row_id",),
            missed=(),
            applicable=True,
            all_excluded=True,
            extra=True,  # type: ignore[call-arg]
        )


def test_evaluate_uptake_records_missed_identifiers_and_module_citations(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake

    packet = _eligible_packet(tmp_path)
    column_index = _module_index(packet, "column-profile")
    traps_index = _module_index(packet, "data-traps")
    decision = _decision(
        packet,
        excluded_columns=(),
        factual_claims=(
            {
                "claim_id": "c1",
                "statement": "row_id is unique",
                "predicate": "likely_id",
                "arguments": {"column": "row_id"},
                "asserted_value": True,
                "evidence": (
                    {"kind": "packet_json_pointer", "pointer": f"/modules/{column_index}/content"},
                    {"kind": "packet_json_pointer", "pointer": f"/modules/{traps_index}/content"},
                    {"kind": "packet_json_pointer", "pointer": "/modules/not-valid"},
                    {"kind": "tool_call", "evidence_id": "sql-1"},
                ),
            },
        ),
    )
    record = evaluate_uptake(
        decision,
        packet,
        arm=Arm.packet_and_full_data,
        repetition_id="repetition-002",
        target_column="label",
    )
    assert record.arm is Arm.packet_and_full_data
    assert record.repetition_id == "repetition-002"
    assert record.identifiers.excluded == ()
    assert record.identifiers.missed == ("row_id",)
    assert record.identifiers.applicable is True
    assert record.identifiers.all_excluded is False
    assert record.packet_module_citations is not None
    assert record.packet_module_citations.column_profile is True
    assert record.packet_module_citations.feature_risks is False
    assert record.packet_module_citations.data_traps is True


def test_imbalance_not_applicable_without_target_class_imbalance_trap(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake

    packet = _rebuild_packet(_eligible_packet(tmp_path), "data-traps", [])
    record = evaluate_uptake(
        _decision(packet, reasoning="no issues"),
        packet,
        arm=Arm.dsx_packet,
        repetition_id="repetition-001",
        target_column="label",
    )
    assert record.imbalance.applicable is False
    assert record.imbalance.acknowledged is None


def _scripted_decision() -> str:
    return canonical_json(
        {
            "primary_metric": "recall_at_5_percent",
            "supporting_metrics": ["precision_at_5_percent"],
            "review_budget_fraction": 0.05,
            "split_strategy": "stratified validation",
            "excluded_columns": ["row_id"],
            "reasoning": "The data is rare-event ranking.",
            "limitations": ["synthetic"],
            "recommendation": "rank cases",
            "factual_claims": [],
            "narrative_claim_ids": [],
        }
    )


def _scripted_reply(*, decision: str | None = None) -> ResponsesEnvelope:
    return ResponsesEnvelope(
        response_id="response",
        status="completed",
        raw_envelope_json=canonical_json({"output": []}),
        usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
        function_calls=(),
        decision_json=decision,
        continuation_items_json=(),
    )


def _scripted_run_root(tmp_path: Path, replies: list[object]) -> Path:
    source = write_csv(tmp_path / "source.csv", _eligible_rows())
    freeze = freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze",
        case_id="case-a",
        target_column="label",
        source_id="src",
    )
    packet_value = json.loads((tmp_path / "freeze" / "packet.json").read_text(encoding="utf-8"))
    manifest = prepare_manifest(
        case=freeze.case,
        packet=OpaquePacket.from_value(packet_value),
        model=ModelConfig(model_identifier="test", system_prompt="Return JSON."),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=1),
            source="test",
            effective_date="2026-08-26",
        ),
        limits=ExperimentLimits(repetitions=1),
        packet_build_metrics=freeze.packet_build_metrics,
        database_path=tmp_path / "inputs" / "dataset.duckdb",
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    run_manifest = DataAccessRunManifest(
        run_manifest_version="data-access-run-manifest-v2",
        input_manifest=manifest,
        input_manifest_digest=canonical_digest(manifest),
        order_seed=1,
    )
    (run_root / RUN_MANIFEST_FILENAME).write_text(
        run_manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    run_experiment(
        manifest=manifest,
        run_root=run_root,
        client=ScriptedResponsesClient(replies),
        order_seed=1,
    )
    return run_root


def _completed_run_root(tmp_path: Path) -> Path:
    return _scripted_run_root(tmp_path, [_scripted_reply(decision=_scripted_decision())] * 3)


def test_evaluate_run_uptake_returns_one_record_per_completed_arm(tmp_path: Path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_run_uptake

    records = evaluate_run_uptake(_completed_run_root(tmp_path))
    assert tuple(record.arm for record in records) == (
        Arm.dsx_packet,
        Arm.full_data,
        Arm.packet_and_full_data,
    )
    assert all(record.repetition_id == "repetition-001" for record in records)
    assert all(record.identifiers.columns == ("row_id",) for record in records)
    assert records[0].packet_module_citations is not None
    assert records[1].packet_module_citations is None


def test_write_uptake_report_creates_exclusive_uptake_json(tmp_path: Path) -> None:
    from dsx.experiments.data_access.uptake import UptakeReport, write_uptake_report

    output = tmp_path / "uptake"
    write_uptake_report(UptakeReport(records=()), output)
    assert (output / "uptake.json").is_file()
    with pytest.raises(FileExistsError):
        write_uptake_report(UptakeReport(records=()), output)
    assert (output / "uptake.json").is_file()


def test_evaluate_run_uptake_rejects_committed_non_packet(tmp_path: Path) -> None:
    from dsx.experiments.data_access.models import CaseConfig, DatasetFormat
    from dsx.experiments.data_access.uptake import evaluate_run_uptake

    source = tmp_path / "source.csv"
    source.write_text("row_id,label\na,0\nb,1\n", encoding="utf-8")
    manifest = prepare_manifest(
        case=CaseConfig(
            case_id="case",
            task_prompt="Recommend a plan.",
            dataset_path=str(source),
            dataset_format=DatasetFormat.csv,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"finding": {"id": "row_id"}}),
        model=ModelConfig(model_identifier="test", system_prompt="Return JSON."),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=1),
            source="test",
            effective_date="2026-08-26",
        ),
        database_path=tmp_path / "dataset.duckdb",
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    run_manifest = DataAccessRunManifest(
        run_manifest_version="data-access-run-manifest-v2",
        input_manifest=manifest,
        input_manifest_digest=canonical_digest(manifest),
        order_seed=1,
    )
    (run_root / RUN_MANIFEST_FILENAME).write_text(
        run_manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="committed packet is not a DSX Packet"):
        evaluate_run_uptake(run_root)


def test_evaluate_run_uptake_skips_incomplete_repetitions(tmp_path: Path) -> None:
    from dsx.experiments.data_access.execution import ModelTransportError
    from dsx.experiments.data_access.uptake import evaluate_run_uptake

    records = evaluate_run_uptake(
        _scripted_run_root(tmp_path, [ModelTransportError("offline")] * 6)
    )
    assert records == ()


def test_write_uptake_report_removes_partial_directory_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsx.experiments.data_access.uptake import UptakeReport, write_uptake_report

    original_open = Path.open

    def fail_json(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "uptake.json":
            raise OSError("disk full")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_json)
    output = tmp_path / "uptake"
    with pytest.raises(OSError, match="disk full"):
        write_uptake_report(UptakeReport(records=()), output)
    assert not output.exists()
