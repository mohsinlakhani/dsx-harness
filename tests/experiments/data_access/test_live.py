"""Opt-in paid smoke coverage for the real Data Access Responses contract."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dsx.experiments.data_access.execution import (
    ExecutionOutcome,
    OpenAIResponsesClient,
    RepetitionOutcome,
    run_repetition,
)
from dsx.experiments.data_access.models import (
    CaseConfig,
    DatasetFormat,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest


def _required_live_setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for the Data Access live smoke test")
    return value


@pytest.mark.live
def test_one_real_data_access_repetition(tmp_path: Path) -> None:
    """Exercise one three-arm Data Access repetition against the live Responses API.

    This is intentionally double-opt-in: set ``DATA_ACCESS_LIVE=1``, ``OPENAI_API_KEY``,
    and ``MODEL_ID``.  It never runs in the normal offline gate.
    """
    if os.environ.get("DATA_ACCESS_LIVE") != "1":
        pytest.skip("set DATA_ACCESS_LIVE=1 to permit paid live smoke coverage")
    model_identifier = _required_live_setting("MODEL_ID")
    _required_live_setting("OPENAI_API_KEY")
    source = tmp_path / "tiny-pilot.json"
    source.write_text(
        json.dumps(
            {
                "rows": [
                    {"row_id": "case-0", "label": 0, "signal": 0.1},
                    {"row_id": "case-1", "label": 0, "signal": 0.2},
                    {"row_id": "case-2", "label": 0, "signal": 0.3},
                    {"row_id": "case-3", "label": 1, "signal": 0.9},
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = prepare_manifest(
        case=CaseConfig(
            case_id="live-smoke",
            task_prompt=(
                "Recommend a classifier for a 5% manual-review budget. Return the required "
                "structured decision. Use factual_claims only for facts you can cite exactly."
            ),
            dataset_path=str(source),
            dataset_format=DatasetFormat.pilot_case_json,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"facts": {"row_count": 4}}),
        model=ModelConfig(
            model_identifier=model_identifier,
            system_prompt="Return only the required structured Data Access decision.",
            max_output_tokens=1024,
        ),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=0),
            output=TokenPrice(usd_per_million_tokens=0),
            source="live-smoke-unpriced",
            effective_date="2026-08-26",
        ),
        limits=ExperimentLimits(
            repetitions=1,
            model_calls_per_arm=4,
            sql_attempts_per_arm=3,
            arm_wall_clock_seconds=90,
            sql_timeout_seconds=5,
            sql_max_rows=20,
            sql_max_result_bytes=8 * 1024,
        ),
        database_path=tmp_path / "dataset.duckdb",
    )
    repetition = run_repetition(
        manifest,
        run_root=tmp_path / "run",
        repetition_number=1,
        repetition_id="live-smoke",
        client=OpenAIResponsesClient(),
        order_seed=1,
        attempt_id_factory=iter(("live-attempt-1", "live-attempt-2")).__next__,
    )
    assert repetition.outcome is RepetitionOutcome.complete, repetition
    for arm_run in repetition.attempts[-1].arms:
        arm_directory = (
            tmp_path
            / "run"
            / "repetition-1-live-smoke"
            / "attempts"
            / repetition.attempts[-1].attempt_id
            / "arms"
            / arm_run.arm.value
        )
        assert (arm_directory / "arm_run.json").is_file()
        assert list((arm_directory / "model_requests").glob("*.json"))
        assert list((arm_directory / "model_calls").glob("*.json"))
        if arm_run.terminal_outcome is ExecutionOutcome.completed:
            assert arm_run.decision_json is not None
        else:
            assert arm_run.decision_json is None
            assert arm_run.terminal_outcome not in {
                ExecutionOutcome.provider_error,
                ExecutionOutcome.transport_error,
            }
