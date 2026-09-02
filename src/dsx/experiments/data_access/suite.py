"""Suite loop over frozen Data Access cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .canonical import canonical_digest
from .execution import ResponsesClient, run_experiment
from .models import (
    DATABASE_FILENAME,
    DEFAULT_SYSTEM_PROMPT,
    MANIFEST_FILENAME,
    RUN_MANIFEST_FILENAME,
    CaseConfig,
    DataAccessContract,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PacketBuildMetrics,
    PricingSnapshot,
)
from .prepare import prepare_manifest

SUITE_INDEX_FILENAME = "suite-index.json"


class SuiteCase(DataAccessContract):
    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    freeze_directory: str
    order_seed: int


class SuiteConfig(DataAccessContract):
    study_id: Literal["data-access-luna-realistic"]
    model_identifier: str
    pricing_path: str
    cases: tuple[SuiteCase, ...] = Field(min_length=1)


class SuiteCaseResult(DataAccessContract):
    case_id: str
    status: Literal["complete", "failed"]
    freeze_directory: str
    input_directory: str | None = None
    run_root: str | None = None
    error: str | None = None


class SuiteIndex(DataAccessContract):
    study_id: Literal["data-access-luna-realistic"]
    cases: tuple[SuiteCaseResult, ...]


def _write_contract(path: Path, contract: DataAccessContract) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _run_one_case(
    case: SuiteCase,
    *,
    output: Path,
    client: ResponsesClient,
    limits: ExperimentLimits,
    system_prompt: str,
    model_identifier: str,
    pricing: PricingSnapshot,
) -> SuiteCaseResult:
    input_directory: str | None = None
    run_root: str | None = None
    try:
        freeze = Path(case.freeze_directory)
        case_config = CaseConfig.model_validate_json(
            (freeze / "case.json").read_text(encoding="utf-8")
        )
        if case_config.case_id != case.case_id:
            raise ValueError(
                "suite case_id "
                f"{case.case_id!r} does not match freeze case.json case_id "
                f"{case_config.case_id!r}"
            )
        packet = OpaquePacket.from_value(
            json.loads((freeze / "packet.json").read_text(encoding="utf-8"))
        )
        metrics = PacketBuildMetrics.model_validate_json(
            (freeze / "packet-build-metrics.json").read_text(encoding="utf-8")
        )
        inputs = output / case.case_id / "inputs"
        manifest = prepare_manifest(
            case=case_config,
            packet=packet,
            model=ModelConfig(model_identifier=model_identifier, system_prompt=system_prompt),
            pricing=pricing,
            limits=limits,
            packet_build_metrics=metrics,
            database_path=inputs / DATABASE_FILENAME,
        )
        _write_contract(inputs / MANIFEST_FILENAME, manifest)
        input_directory = str(inputs)
        run_dir = output / case.case_id / "run"
        run_dir.mkdir()
        from .cli import DataAccessRunManifest

        run_manifest = DataAccessRunManifest(
            run_manifest_version="data-access-run-manifest-v2",
            input_manifest=manifest,
            input_manifest_digest=canonical_digest(manifest),
            order_seed=case.order_seed,
        )
        _write_contract(run_dir / RUN_MANIFEST_FILENAME, run_manifest)
        run_root = str(run_dir)
        run_experiment(
            manifest=manifest,
            run_root=run_dir,
            client=client,
            order_seed=case.order_seed,
        )
    except Exception as error:
        return SuiteCaseResult(
            case_id=case.case_id,
            status="failed",
            freeze_directory=case.freeze_directory,
            input_directory=input_directory,
            run_root=run_root,
            error=str(error),
        )
    return SuiteCaseResult(
        case_id=case.case_id,
        status="complete",
        freeze_directory=case.freeze_directory,
        input_directory=input_directory,
        run_root=run_root,
    )


def run_suite(
    config: SuiteConfig,
    output: Path,
    *,
    client: ResponsesClient,
    limits: ExperimentLimits | None = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> SuiteIndex:
    """Prepare and run each frozen case into an exclusive suite directory."""
    active_limits = limits or ExperimentLimits()
    if output.exists():
        raise FileExistsError(output)
    pricing = PricingSnapshot.model_validate_json(
        Path(config.pricing_path).read_text(encoding="utf-8")
    )
    output.mkdir(parents=True, exist_ok=False)
    results = tuple(
        _run_one_case(
            case,
            output=output,
            client=client,
            limits=active_limits,
            system_prompt=system_prompt,
            model_identifier=config.model_identifier,
            pricing=pricing,
        )
        for case in config.cases
    )
    index = SuiteIndex(study_id=config.study_id, cases=results)
    _write_contract(output / SUITE_INDEX_FILENAME, index)
    return index
