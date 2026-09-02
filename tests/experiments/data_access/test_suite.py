from __future__ import annotations

from pathlib import Path

import pytest

from dsx.experiments.data_access.canonical import canonical_json
from dsx.experiments.data_access.execution import (
    FunctionCall,
    ResponsesEnvelope,
    ScriptedResponsesClient,
    Usage,
)
from dsx.experiments.data_access.models import ExperimentLimits, PricingSnapshot, TokenPrice
from dsx.experiments.data_access.realistic import freeze_case
from tests.builders.helpers import write_csv


def _eligible_rows() -> list[dict[str, object]]:
    return [
        {
            "row_id": f"r{index}",
            "label": 1 if index == 0 else 0,
            "nullable": None if index < 2 else index,
            "noise": 0,
        }
        for index in range(20)
    ]


def _decision() -> str:
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


def _reply(*, decision: str | None = None, call: FunctionCall | None = None) -> ResponsesEnvelope:
    output: list[dict[str, object]] = []
    if call is not None:
        output.append(
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments_json,
            }
        )
    return ResponsesEnvelope(
        response_id="response",
        status="completed",
        raw_envelope_json=canonical_json({"output": output}),
        usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
        function_calls=(call,) if call else (),
        decision_json=decision,
        continuation_items_json=tuple(canonical_json(item) for item in output),
    )


def _pricing(path: Path) -> Path:
    path.write_text(
        PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1.0),
            output=TokenPrice(usd_per_million_tokens=2.0),
            source="test",
            effective_date="2026-09-02",
        ).model_dump_json(),
        encoding="utf-8",
    )
    return path


def _scripted_client() -> ScriptedResponsesClient:
    return ScriptedResponsesClient([_reply(decision=_decision())] * 3)


def _limits() -> ExperimentLimits:
    return ExperimentLimits(
        repetitions=1,
        model_calls_per_arm=4,
        sql_attempts_per_arm=3,
        arm_wall_clock_seconds=30,
    )


def test_run_suite_prepares_and_runs_one_frozen_case(tmp_path: Path) -> None:
    from dsx.experiments.data_access.suite import SuiteCase, SuiteConfig, run_suite

    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    freeze = freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze",
        case_id="case-a",
        target_column="label",
        source_id="src",
    )
    pricing = _pricing(tmp_path / "pricing.json")
    config = SuiteConfig(
        study_id="data-access-luna-realistic",
        model_identifier="gpt-5.6-luna",
        pricing_path=str(pricing),
        cases=(
            SuiteCase(case_id="case-a", freeze_directory=str(tmp_path / "freeze"), order_seed=7),
        ),
    )
    index = run_suite(
        config,
        tmp_path / "suite",
        client=_scripted_client(),
        limits=_limits(),
    )
    assert index.cases[0].status == "complete"
    assert (tmp_path / "suite" / "suite-index.json").is_file()
    assert freeze.case.case_id == "case-a"
    assert (tmp_path / "suite" / "case-a" / "inputs" / "manifest.json").is_file()
    assert (tmp_path / "suite" / "case-a" / "run" / "run_manifest.json").is_file()


def test_run_suite_records_failed_case_without_deleting_completed_sibling(tmp_path: Path) -> None:
    from dsx.experiments.data_access.suite import SuiteCase, SuiteConfig, run_suite

    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze-a",
        case_id="case-a",
        target_column="label",
        source_id="src",
    )
    pricing = _pricing(tmp_path / "pricing.json")
    config = SuiteConfig(
        study_id="data-access-luna-realistic",
        model_identifier="gpt-5.6-luna",
        pricing_path=str(pricing),
        cases=(
            SuiteCase(
                case_id="case-a",
                freeze_directory=str(tmp_path / "freeze-a"),
                order_seed=7,
            ),
            SuiteCase(
                case_id="case-b",
                freeze_directory=str(tmp_path / "missing-freeze"),
                order_seed=8,
            ),
        ),
    )
    index = run_suite(
        config,
        tmp_path / "suite",
        client=_scripted_client(),
        limits=_limits(),
    )
    assert [result.status for result in index.cases] == ["complete", "failed"]
    assert index.cases[1].error is not None
    assert (tmp_path / "suite" / "case-a" / "run").is_dir()
    assert index.cases[0].run_root is not None
    assert Path(index.cases[0].run_root).is_dir()
    assert (tmp_path / "suite" / "suite-index.json").is_file()


def test_suite_case_id_rejects_values_outside_case_config_pattern() -> None:
    from pydantic import ValidationError

    from dsx.experiments.data_access.suite import SuiteCase

    with pytest.raises(ValidationError, match="case_id"):
        SuiteCase(case_id="has space", freeze_directory="freeze", order_seed=1)


def test_run_suite_records_case_id_mismatch_as_failed(tmp_path: Path) -> None:
    from dsx.experiments.data_access.suite import SuiteCase, SuiteConfig, run_suite

    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze-a",
        case_id="case-a",
        target_column="label",
        source_id="src",
    )
    pricing = _pricing(tmp_path / "pricing.json")
    config = SuiteConfig(
        study_id="data-access-luna-realistic",
        model_identifier="gpt-5.6-luna",
        pricing_path=str(pricing),
        cases=(
            SuiteCase(
                case_id="case-b",
                freeze_directory=str(tmp_path / "freeze-a"),
                order_seed=7,
            ),
        ),
    )
    index = run_suite(
        config,
        tmp_path / "suite",
        client=_scripted_client(),
        limits=_limits(),
    )
    assert index.cases[0].status == "failed"
    assert index.cases[0].error is not None
    assert "case-a" in index.cases[0].error
    assert "case-b" in index.cases[0].error
    assert (tmp_path / "suite" / "suite-index.json").is_file()


def test_run_suite_refuses_existing_output(tmp_path: Path) -> None:
    from dsx.experiments.data_access.suite import SuiteCase, SuiteConfig, run_suite

    output = tmp_path / "suite"
    output.mkdir()
    config = SuiteConfig(
        study_id="data-access-luna-realistic",
        model_identifier="gpt-5.6-luna",
        pricing_path=str(tmp_path / "pricing.json"),
        cases=(
            SuiteCase(case_id="case-a", freeze_directory=str(tmp_path / "freeze"), order_seed=1),
        ),
    )
    with pytest.raises(FileExistsError):
        run_suite(config, output, client=_scripted_client())
    assert list(output.iterdir()) == []
