"""Public command-line boundary for the proof-first pilot workflow."""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import TypeAdapter, ValidationError

from .blind import (
    FROZEN_JUDGMENTS_FILENAME,
    MANIFEST_FILENAME,
    REVEAL_DIRECTORY,
    REVEAL_MAP_FILENAME,
    REVEALED_REPORT_FILENAME,
    export_blind,
    freeze_judgments,
    reveal_blind,
)
from .models import (
    FROZEN_CASE_DIGEST,
    Arm,
    ArmOutcome,
    BlindJudgment,
    BlindManifest,
    Packet,
    PairSummary,
    PilotCase,
    PilotContract,
    RunManifest,
    candidate_packet,
    canonical_case_digest,
    generate_pilot_case,
)
from .render import (
    RenderedRequests,
    RequestConfiguration,
    prove_controlled_delta,
    render_requests,
    request_digest,
)
from .runner import OpenAIModelClient, run_pair

DEFAULT_GENERATION_SEED = 20260819
DEFAULT_SYSTEM_PROMPT = (
    "Return only a valid structured analysis matching the requested response schema."
)
DEFAULT_RESPONSE_SCHEMA_NAME = "analysis_decision"
INTENDED_PAIR_COUNT = 3

CASE_FILENAME = "case.json"
PACKET_FILENAME = "packet.json"
CONFIGURATION_FILENAME = "request_configuration.json"
RENDERED_FILENAME = "rendered_requests.json"
RUN_MANIFEST_FILENAME = "run_manifest.json"

app = typer.Typer(
    add_completion=False,
    help="Run the one-case proof-first DSX information-availability pilot.",
    no_args_is_help=True,
)


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE settings without overriding the real environment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError:
        return

    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and value and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


@app.callback()
def load_environment() -> None:
    """Load optional local configuration before running a command."""
    _load_dotenv(Path.cwd() / ".env")


def _abort(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _write_contract(path: Path, contract: PilotContract) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _load_contract[ContractT: PilotContract](
    path: Path, contract_type: type[ContractT]
) -> ContractT:
    return contract_type.model_validate_json(path.read_text(encoding="utf-8"))


def _create_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        _abort(f"{label} already exists: {path}")
    except OSError:
        _abort(f"could not create {label}: {path}")


def _load_generated(
    generated_directory: Path,
) -> tuple[PilotCase, Packet, RequestConfiguration, RenderedRequests]:
    try:
        case = _load_contract(generated_directory / CASE_FILENAME, PilotCase)
        packet = _load_contract(generated_directory / PACKET_FILENAME, Packet)
        configuration = _load_contract(
            generated_directory / CONFIGURATION_FILENAME, RequestConfiguration
        )
        persisted_rendered = _load_contract(
            generated_directory / RENDERED_FILENAME, RenderedRequests
        )
    except (OSError, ValidationError) as error:
        raise ValueError("invalid generated artifact") from error

    regenerated_case = generate_pilot_case(case.generation_seed)
    persisted_case_digest = canonical_case_digest(case)
    expected_case_digest = canonical_case_digest(regenerated_case)
    if case.generation_seed == DEFAULT_GENERATION_SEED and (
        expected_case_digest != FROZEN_CASE_DIGEST
    ):
        raise ValueError("default frozen case digest verification failed")
    if persisted_case_digest != expected_case_digest:
        raise ValueError("generated case digest mismatch")
    if packet != candidate_packet():
        raise ValueError("generated packet mismatch")

    rendered = render_requests(case, packet, configuration)
    if persisted_rendered != rendered:
        raise ValueError("generated rendered request digest mismatch")
    return case, packet, configuration, rendered


def _pair_id(case_digest: str, base_order_seed: int, pair_number: int) -> str:
    material = f"dsx-pilot:v1:{case_digest}:{base_order_seed}:{pair_number}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _pair_order_seed(base_order_seed: int, pair_number: int) -> int:
    material = f"dsx-pilot:order:v1:{base_order_seed}:{pair_number}"
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


def _load_run_manifest(run_root: Path) -> RunManifest:
    try:
        return _load_contract(run_root / RUN_MANIFEST_FILENAME, RunManifest)
    except (OSError, ValidationError) as error:
        raise ValueError("invalid run root or run manifest") from error


def _validate_run_artifacts(run_root: Path, manifest: RunManifest) -> None:
    """Bind terminal pair artifacts to every identity and request commitment."""
    expected_pair_ids = tuple(
        _pair_id(manifest.case_digest, manifest.base_order_seed, pair_number)
        for pair_number in range(1, manifest.intended_pair_count + 1)
    )
    expected_order_seeds = tuple(
        _pair_order_seed(manifest.base_order_seed, pair_number)
        for pair_number in range(1, manifest.intended_pair_count + 1)
    )
    expected_paths = tuple(
        run_root / f"pair-{pair_number}-{pair_id}" / "pair_summary.json"
        for pair_number, pair_id in enumerate(manifest.pair_ids, start=1)
    )
    discovered_paths = tuple(sorted(run_root.glob("pair-*/pair_summary.json")))
    if (
        manifest.pair_ids != expected_pair_ids
        or manifest.pair_order_seeds != expected_order_seeds
        or set(discovered_paths) != set(expected_paths)
    ):
        raise ValueError("run artifacts do not match run manifest")

    case = generate_pilot_case(manifest.generation_seed)
    packet = candidate_packet()
    if (
        canonical_case_digest(case) != manifest.case_digest
        or packet.version != manifest.packet_version
    ):
        raise ValueError("run artifacts do not match run manifest")

    try:
        for pair_number, (pair_id, order_seed, summary_path) in enumerate(
            zip(
                manifest.pair_ids,
                manifest.pair_order_seeds,
                expected_paths,
                strict=True,
            ),
            start=1,
        ):
            summary = _load_contract(summary_path, PairSummary)
            if summary.pair_number != pair_number or summary.pair_id != pair_id:
                raise ValueError("run artifacts do not match run manifest")
            attempts_directory = summary_path.parent / "attempts"
            persisted_attempt_paths = tuple(
                sorted(path for path in attempts_directory.iterdir() if path.is_dir())
            )
            persisted_outcomes_by_attempt = {
                attempt_directory: {
                    path: _load_contract(path, ArmOutcome)
                    for path in sorted((attempt_directory / "outcomes").glob("*.json"))
                }
                for attempt_directory in persisted_attempt_paths
            }
            expected_attempt_paths = {
                attempts_directory / attempt.start.attempt_id for attempt in summary.attempts
            }
            if set(persisted_attempt_paths) != expected_attempt_paths:
                raise ValueError("run artifacts do not match run manifest")
            order_rng = random.Random(order_seed)
            for attempt in summary.attempts:
                expected_order = tuple(order_rng.sample(tuple(Arm), k=2))
                if attempt.start.arm_order != expected_order:
                    raise ValueError("run artifacts do not match run manifest")
                attempt_directory = summary_path.parent / "attempts" / attempt.start.attempt_id
                persisted_start = _load_contract(
                    attempt_directory / "attempt_start.json", type(attempt.start)
                )
                persisted_summary = _load_contract(
                    attempt_directory / "attempt_summary.json", type(attempt)
                )
                outcome_directory = attempt_directory / "outcomes"
                persisted_outcomes = persisted_outcomes_by_attempt[attempt_directory]
                expected_outcomes = {
                    outcome_directory
                    / f"{outcome.request_number:02d}-{outcome.arm.value}.json": outcome
                    for outcome in attempt.outcomes
                }
                rendered = _load_contract(
                    attempt_directory / "rendered_requests.json", RenderedRequests
                )
                off_request_digest = request_digest(rendered.packet_off.request)
                on_request_digest = request_digest(rendered.packet_on.request)
                common_digest = prove_controlled_delta(
                    rendered.packet_off.request,
                    rendered.packet_on.request,
                    expected_packet=packet,
                )
                if (
                    persisted_start != attempt.start
                    or persisted_summary != attempt
                    or persisted_outcomes != expected_outcomes
                    or rendered.packet_off.request.task_prompt != case.task_text
                    or rendered.packet_on.request.task_prompt != case.task_text
                    or rendered.packet_off.request.model_identifier
                    != manifest.model_identifier
                    or rendered.packet_on.request.model_identifier
                    != manifest.model_identifier
                    or rendered.packet_off.request_digest != off_request_digest
                    or off_request_digest != manifest.packet_off_request_digest
                    or rendered.packet_on.request_digest != on_request_digest
                    or on_request_digest != manifest.packet_on_request_digest
                    or rendered.common_projection_digest != common_digest
                    or rendered.packet_off.common_projection_digest != common_digest
                    or rendered.packet_on.common_projection_digest != common_digest
                    or common_digest != manifest.common_projection_digest
                    or attempt.start.common_projection_digest != common_digest
                    or any(
                        outcome.request_digest
                        != (
                            manifest.packet_off_request_digest
                            if outcome.arm is Arm.packet_off
                            else manifest.packet_on_request_digest
                        )
                        for outcome in attempt.outcomes
                    )
                ):
                    raise ValueError("run artifacts do not match run manifest")
    except (OSError, ValidationError, ValueError) as error:
        raise ValueError("run artifacts do not match run manifest") from error


def _load_blind_manifest(bundle: Path) -> BlindManifest:
    try:
        return _load_contract(bundle / MANIFEST_FILENAME, BlindManifest)
    except (OSError, ValidationError) as error:
        raise ValueError("invalid or missing blind bundle manifest") from error


@app.command("generate")
def generate(
    output_directory: Annotated[
        Path, typer.Argument(help="New directory for generated pilot artifacts.")
    ],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Exact model identifier (defaults to MODEL_ID)."),
    ] = None,
    seed: Annotated[
        int, typer.Option("--seed", help="Deterministic pilot-case generation seed.")
    ] = DEFAULT_GENERATION_SEED,
    system_prompt: Annotated[
        str, typer.Option("--system-prompt", help="Shared system prompt for both arms.")
    ] = DEFAULT_SYSTEM_PROMPT,
    response_schema_name: Annotated[
        str,
        typer.Option("--response-schema-name", help="Structured response schema name."),
    ] = DEFAULT_RESPONSE_SCHEMA_NAME,
) -> None:
    """Generate, render, prove, and persist a new immutable pilot input bundle."""
    model_identifier = os.environ.get("MODEL_ID") if model is None else model
    if model is None and not model_identifier:
        _abort("a model identifier is required; pass --model or set MODEL_ID in .env")
    assert model_identifier is not None
    case = generate_pilot_case(seed)
    packet = candidate_packet()
    try:
        configuration = RequestConfiguration(
            model_identifier=model_identifier,
            system_prompt=system_prompt,
            response_schema_name=response_schema_name,
        )
        rendered = render_requests(case, packet, configuration)
    except (ValidationError, ValueError) as error:
        _abort(f"invalid request configuration: {str(error).splitlines()[0]}")

    case_digest = canonical_case_digest(case)
    if seed == DEFAULT_GENERATION_SEED and case_digest != FROZEN_CASE_DIGEST:
        _abort("default frozen case digest verification failed")

    _create_directory(output_directory, "output directory")
    try:
        _write_contract(output_directory / CASE_FILENAME, case)
        _write_contract(output_directory / PACKET_FILENAME, packet)
        _write_contract(output_directory / CONFIGURATION_FILENAME, configuration)
        _write_contract(output_directory / RENDERED_FILENAME, rendered)
    except OSError:
        _abort(f"could not write generated artifacts in {output_directory}")

    typer.echo(f"Generated artifacts: {output_directory}")
    typer.echo(f"Case digest: {case_digest}")
    typer.echo(
        "Request digests: "
        f"packet_off={rendered.packet_off.request_digest} "
        f"packet_on={rendered.packet_on.request_digest}"
    )
    typer.echo(f"Common projection digest: {rendered.common_projection_digest}")


@app.command("run")
def run(
    generated_directory: Annotated[
        Path, typer.Argument(help="Directory created by the generate command.")
    ],
    run_root: Annotated[Path, typer.Argument(help="New exclusive live-run directory.")],
    order_seed: Annotated[
        int,
        typer.Option(
            "--order-seed",
            help="Required base seed used to derive and freeze each pair's arm order.",
        ),
    ],
) -> None:
    """Verify generated inputs and execute exactly three sequential live pairs."""
    if os.path.lexists(run_root):
        _abort(f"run root already exists: {run_root}")
    try:
        case, packet, configuration, rendered = _load_generated(generated_directory)
    except ValueError as error:
        _abort(str(error))
    if not os.environ.get("OPENAI_API_KEY"):
        _abort("OPENAI_API_KEY is required for live execution")

    case_digest = canonical_case_digest(case)
    pair_ids = tuple(
        _pair_id(case_digest, order_seed, number)
        for number in range(1, INTENDED_PAIR_COUNT + 1)
    )
    pair_order_seeds = tuple(
        _pair_order_seed(order_seed, number)
        for number in range(1, INTENDED_PAIR_COUNT + 1)
    )
    manifest = RunManifest(
        case_digest=case_digest,
        packet_version=packet.version,
        model_identifier=configuration.model_identifier,
        generation_seed=case.generation_seed,
        base_order_seed=order_seed,
        pair_ids=pair_ids,
        pair_order_seeds=pair_order_seeds,
        packet_off_request_digest=rendered.packet_off.request_digest,
        packet_on_request_digest=rendered.packet_on.request_digest,
        common_projection_digest=rendered.common_projection_digest,
    )
    _create_directory(run_root, "run root")
    try:
        _write_contract(run_root / RUN_MANIFEST_FILENAME, manifest)
    except OSError:
        _abort(f"could not write run manifest in {run_root}")

    client = OpenAIModelClient()
    for pair_number, (pair_id, pair_seed) in enumerate(
        zip(pair_ids, pair_order_seeds, strict=True), start=1
    ):
        summary = run_pair(
            rendered,
            run_root=run_root,
            pair_number=pair_number,
            pair_id=pair_id,
            client=client,
            order_seed=pair_seed,
            attempt_id_factory=lambda: secrets.token_urlsafe(18),
        )
        typer.echo(f"Pair {pair_number} ({pair_id}): {summary.outcome_kind.value}")


def _judgment_template() -> dict[str, object]:
    return {
        "opaque_id": "<opaque-id>",
        "decision_quality": 1,
        "evidence_use": 1,
        "limitations_quality": 1,
        "packet_guess": "packet_off",
        "guess_confidence": 1,
        "metric_reasoning": 1,
        "split_strategy": 1,
        "leakage_row_id_avoidance": 1,
        "limitations": 1,
        "overall_recommendation_quality": 1,
        "exact_prevalence_recognition": 1,
        "majority_baseline_recognition": 1,
        "citation_use": 1,
    }


@app.command("judge")
def judge(
    run_root: Annotated[Path, typer.Argument(help="Private source run directory.")],
    blind_bundle: Annotated[
        Path, typer.Argument(help="Public blind bundle directory.")
    ],
    blind_seed: Annotated[
        int, typer.Option("--blind-seed", help="Required public blind permutation seed.")
    ],
    judgments: Annotated[
        Path | None,
        typer.Option("--judgments", help="JSON array of complete blind judgments to freeze."),
    ] = None,
) -> None:
    """Export a blind bundle or freeze complete judgments without revealing labels."""
    try:
        run_manifest = _load_run_manifest(run_root)
        _validate_run_artifacts(run_root, run_manifest)
    except ValueError as error:
        _abort(str(error))

    if judgments is None:
        try:
            manifest = export_blind(run_root, blind_bundle, blind_seed=blind_seed)
        except FileExistsError:
            _abort(f"blind bundle already exists: {blind_bundle}")
        except (OSError, ValidationError, ValueError):
            _abort("could not export a valid blind bundle")
        typer.echo("Ordered opaque IDs:")
        for opaque_id in manifest.ordered_opaque_ids:
            typer.echo(f"  {opaque_id}")
        typer.echo("Expected judgment JSON shape (all scores are integers from 1 to 5):")
        typer.echo(json.dumps([_judgment_template()], indent=2))
        typer.echo(
            "Do not give evaluators source run-root access: the public seed plus "
            "labeled source artifacts can reconstruct treatment labels."
        )
        return

    try:
        manifest = _load_blind_manifest(blind_bundle)
    except ValueError as error:
        _abort(str(error))
    if manifest.blind_seed != blind_seed:
        _abort("blind seed does not match the existing bundle manifest")
    try:
        parsed = TypeAdapter(list[BlindJudgment]).validate_json(
            judgments.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        _abort(f"invalid judgment JSON: {str(error).splitlines()[0]}")
    try:
        raw_judgments: list[BlindJudgment | dict[str, object]] = list(parsed)
        frozen = freeze_judgments(blind_bundle, raw_judgments, run_root=run_root)
    except FileExistsError:
        _abort(
            f"frozen judgment file already exists: "
            f"{blind_bundle / FROZEN_JUDGMENTS_FILENAME}"
        )
    except (OSError, ValidationError, ValueError) as error:
        _abort(f"could not freeze judgments: {str(error).splitlines()[0]}")
    typer.echo(f"Frozen judgments: {blind_bundle / FROZEN_JUDGMENTS_FILENAME}")
    typer.echo(f"Judgments digest: {frozen.judgments_digest}")


@app.command("reveal")
def reveal(
    run_root: Annotated[Path, typer.Argument(help="Private source run directory.")],
    blind_bundle: Annotated[
        Path, typer.Argument(help="Frozen public blind bundle directory.")
    ],
) -> None:
    """Reveal official arm assignments only after a valid judgment freeze."""
    try:
        run_manifest = _load_run_manifest(run_root)
        _validate_run_artifacts(run_root, run_manifest)
    except ValueError as error:
        _abort(str(error))
    if not (blind_bundle / FROZEN_JUDGMENTS_FILENAME).is_file():
        _abort("frozen judgments are required before reveal")
    try:
        _, report = reveal_blind(run_root, blind_bundle)
    except FileExistsError:
        _abort(f"reveal already exists: {blind_bundle / REVEAL_DIRECTORY}")
    except (OSError, ValidationError, ValueError) as error:
        _abort(f"could not reveal blind results: {str(error).splitlines()[0]}")

    typer.echo(f"Reveal map: {blind_bundle / REVEAL_DIRECTORY / REVEAL_MAP_FILENAME}")
    typer.echo(
        f"Revealed report: {blind_bundle / REVEAL_DIRECTORY / REVEALED_REPORT_FILENAME}"
    )
    typer.echo("Comparative ratings:")
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in report.comparative_ratings], indent=2
        )
    )
    typer.echo("Packet-uptake diagnostics:")
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in report.packet_uptake_diagnostics],
            indent=2,
        )
    )
    typer.echo("Arm-guess results:")
    typer.echo(
        json.dumps(
            [item.model_dump(mode="json") for item in report.arm_guess_results], indent=2
        )
    )
