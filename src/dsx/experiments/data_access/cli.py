"""Command-line boundary for the separate Data Access experiment."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import duckdb
import typer
from pydantic import BaseModel, ValidationError, model_validator

from .canonical import canonical_digest, json_value
from .models import (
    DATABASE_FILENAME,
    DEFAULT_SYSTEM_PROMPT,
    MANIFEST_FILENAME,
    RUN_MANIFEST_FILENAME,
    CaseConfig,
    DataAccessContract,
    DataAccessManifest,
    ExperimentLimits,
    ModelConfig,
    OpaquePacket,
    PacketBuildMetrics,
    PricingSnapshot,
)
from .prepare import prepare_manifest

app = typer.Typer(
    add_completion=False,
    help="Run the Data Access v2 three-arm DSX packet and full-data experiment.",
    no_args_is_help=True,
)


def _load_dotenv(path: Path) -> None:
    """Load simple local settings without replacing values supplied by the shell."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", maxsplit=1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and value and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


@app.callback()
def load_environment() -> None:
    """Load the optional project-local `.env` before command execution."""
    _load_dotenv(Path.cwd() / ".env")


def _abort(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _create_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        _abort(f"{label} already exists: {path}")
    except OSError:
        _abort(f"could not create {label}: {path}")


def _write_contract(path: Path, contract: BaseModel) -> None:
    with path.open("x", encoding="utf-8") as artifact:
        artifact.write(contract.model_dump_json(indent=2))
        artifact.write("\n")


def _load_contract[ContractT: BaseModel](path: Path, model: type[ContractT]) -> ContractT:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


class DataAccessRunManifest(DataAccessContract):
    """Run-root commitment written before a live client is constructed."""

    run_manifest_version: Literal["data-access-run-manifest-v2"]
    input_manifest: DataAccessManifest
    input_manifest_digest: str
    order_seed: int

    @model_validator(mode="after")
    def validate_input_commitment(self) -> DataAccessRunManifest:
        if self.input_manifest_digest != canonical_digest(self.input_manifest):
            raise ValueError("run manifest input digest does not match its manifest")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _materialized_digest(path: Path) -> str:
    """Recompute the committed logical dataset digest without a result-size cap."""
    connection = duckdb.connect(
        str(path), read_only=True, config={"enable_external_access": "false"}
    )
    try:
        description = connection.execute("DESCRIBE dataset").fetchall()
        columns = tuple(str(item[0]) for item in description)
        quoted = ", ".join(_quote_identifier(column) for column in columns)
        rows = connection.execute(f"SELECT {quoted} FROM dataset").fetchall()
        return canonical_digest(
            {
                "columns": [
                    {"name": str(item[0]), "type_name": str(item[1])}
                    for item in description
                ],
                "rows": [list(json_value(row)) for row in rows],
            }
        )
    finally:
        connection.close()


def _validate_prepared_input(input_directory: Path, manifest: DataAccessManifest) -> None:
    """Fail closed if source or materialized data differs from the committed manifest."""
    database = Path(manifest.dataset.database_path)
    if not database.is_file() or database.parent.resolve() != input_directory.resolve():
        raise ValueError("prepared database path does not match input directory")
    source = Path(manifest.case.dataset_path)
    if not source.is_file() or _sha256_file(source) != manifest.dataset.source_digest:
        raise ValueError("source dataset digest does not match manifest")
    if _materialized_digest(database) != manifest.dataset.materialized_digest:
        raise ValueError("materialized dataset digest does not match manifest")


@app.command()
def prepare(
    case_config: Annotated[
        Path, typer.Argument(help="JSON Data Access case configuration.")
    ],
    dsx_packet: Annotated[Path, typer.Argument(help="Arbitrary JSON DSX packet.")],
    output: Annotated[Path, typer.Argument(help="New immutable input directory.")],
    model: Annotated[str, typer.Option("--model", help="Exact model identifier to commit.")],
    pricing: Annotated[Path, typer.Option("--pricing", help="JSON frozen pricing snapshot.")],
    system_prompt: Annotated[
        str, typer.Option(help="Shared system prompt committed for all three arms.")
    ] = DEFAULT_SYSTEM_PROMPT,
    packet_build_metrics: Annotated[
        Path | None,
        typer.Option(help="Optional JSON packet lifecycle timing/token/cost metrics."),
    ] = None,
    repetitions: Annotated[int, typer.Option(min=1, help="Three-arm repetitions.")] = 3,
) -> None:
    """Prepare a packet, source data, pricing, and immutable DuckDB input bundle."""
    _create_directory(output, "output directory")
    try:
        case = _load_contract(case_config, CaseConfig)
        price = _load_contract(pricing, PricingSnapshot)
        packet = OpaquePacket.from_value(_load_json(dsx_packet))
        lifecycle = (
            _load_contract(packet_build_metrics, PacketBuildMetrics)
            if packet_build_metrics is not None
            else None
        )
        manifest = prepare_manifest(
            case=case,
            packet=packet,
            model=ModelConfig(model_identifier=model, system_prompt=system_prompt),
            pricing=price,
            limits=ExperimentLimits(repetitions=repetitions),
            packet_build_metrics=lifecycle,
            database_path=output / DATABASE_FILENAME,
        )
        _write_contract(output / MANIFEST_FILENAME, manifest)
    except (OSError, ValidationError, ValueError, TypeError) as error:
        _abort(f"could not prepare Data Access input: {error}")
    typer.echo(f"Prepared Data Access input: {output}")
    typer.echo(f"Packet digest: {manifest.packet.digest}")
    typer.echo(f"Dataset digest: {manifest.dataset.materialized_digest}")


def _load_manifest(input_directory: Path) -> DataAccessManifest:
    """Read the committed preparation manifest for command adapters and tests."""
    try:
        return _load_contract(input_directory / MANIFEST_FILENAME, DataAccessManifest)
    except (OSError, ValidationError) as error:
        raise ValueError("invalid Data Access input manifest") from error


@app.command()
def run(
    input_directory: Annotated[Path, typer.Argument(help="Directory produced by prepare.")],
    run_root: Annotated[Path, typer.Argument(help="New private run directory.")],
    order_seed: Annotated[int, typer.Option(help="Three-arm order randomization seed.")],
) -> None:
    """Run three live arms per repetition after prepared commitments are revalidated."""
    if not os.environ.get("OPENAI_API_KEY"):
        _abort("OPENAI_API_KEY is required for live Data Access runs")
    _create_directory(run_root, "run root")
    try:
        manifest = _load_manifest(input_directory)
        _validate_prepared_input(input_directory, manifest)
        run_manifest = DataAccessRunManifest(
            run_manifest_version="data-access-run-manifest-v2",
            input_manifest=manifest,
            input_manifest_digest=canonical_digest(manifest),
            order_seed=order_seed,
        )
        _write_contract(run_root / RUN_MANIFEST_FILENAME, run_manifest)
        # Imported lazily so offline preparation remains independent of live execution.
        from .execution import OpenAIResponsesClient, run_experiment

        run_experiment(
            manifest=manifest,
            run_root=run_root,
            client=OpenAIResponsesClient(),
            order_seed=order_seed,
        )
    except (OSError, ValidationError, ValueError) as error:
        _abort(f"could not run Data Access experiment: {error}")
    typer.echo(f"Data Access run complete: {run_root}")


@app.command()
def judge(
    run_root: Annotated[Path, typer.Argument(help="Private Data Access run directory.")],
    blind_bundle: Annotated[Path, typer.Argument(help="New or existing blind bundle.")],
    blind_seed: Annotated[int, typer.Option(help="Public opaque-ID permutation seed.")],
    judgments: Annotated[
        Path | None, typer.Option(help="Complete JSON blind judgments to freeze.")
    ] = None,
) -> None:
    """Export blind decisions or exclusively freeze a complete judgment set."""
    try:
        from .blind import export_blind, freeze_judgments

        if judgments is None:
            manifest = export_blind(run_root, blind_bundle, blind_seed=blind_seed)
            typer.echo(f"Blind bundle exported: {blind_bundle}")
            typer.echo(f"Eligible outputs: {manifest.eligible_count}")
        else:
            from .blind import BlindManifest

            manifest = BlindManifest.model_validate_json(
                (blind_bundle / "manifest.json").read_text(encoding="utf-8")
            )
            if manifest.blind_seed != blind_seed:
                raise ValueError("blind seed does not match the existing blind manifest")
            raw = _load_json(judgments)
            if not isinstance(raw, list):
                raise ValueError("judgments JSON must be an array")
            frozen = freeze_judgments(blind_bundle, raw, run_root=run_root)
            typer.echo(f"Frozen blind judgments: {blind_bundle / 'frozen_judgments.json'}")
            typer.echo(f"Judgments digest: {frozen.judgments_digest}")
    except (OSError, ValidationError, ValueError, TypeError) as error:
        _abort(f"could not judge Data Access run: {error}")


@app.command()
def reveal(
    run_root: Annotated[Path, typer.Argument(help="Private Data Access run directory.")],
    blind_bundle: Annotated[Path, typer.Argument(help="Blind bundle with frozen judgments.")],
) -> None:
    """Publish labels and joined private outcome metrics only after blind freeze."""
    try:
        from .blind import reveal_blind

        _reveal_map, report = reveal_blind(run_root, blind_bundle)
    except (OSError, ValidationError, ValueError, TypeError) as error:
        _abort(f"could not reveal Data Access run: {error}")
    typer.echo(f"Data Access reveal published: {blind_bundle / 'reveal'}")
    typer.echo(f"Revealed outputs: {len(report.raw_judgments)}")


@app.command()
def freeze(
    dataset: Annotated[Path, typer.Argument(help="CSV or Parquet dataset to freeze.")],
    output: Annotated[Path, typer.Argument(help="New exclusive freeze directory.")],
    case_id: Annotated[str, typer.Option("--case-id", help="Case identity for CaseConfig.")],
    target: Annotated[str, typer.Option("--target", help="Target column in the dataset.")],
    source_id: Annotated[
        str, typer.Option("--source-id", help="Provenance identifier for the source.")
    ],
    license_accepted: Annotated[
        bool,
        typer.Option(
            ...,
            "--license-accepted",
            help="Confirm the source dataset license is accepted.",
        ),
    ],
    packet_id: Annotated[
        str | None,
        typer.Option("--packet-id", help="Optional packet identity; defaults to case id."),
    ] = None,
) -> None:
    """Freeze a builder-generated Data Access case from a tabular dataset."""
    try:
        from .realistic import freeze_case

        result = freeze_case(
            dataset_path=dataset,
            output=output,
            case_id=case_id,
            target_column=target,
            source_id=source_id,
            packet_id=packet_id,
        )
    except (FileExistsError, ValueError, OSError, ValidationError, duckdb.Error) as error:
        _abort(f"could not freeze Data Access case: {error}")
    typer.echo(f"Frozen Data Access case: {output}")
    typer.echo(f"Packet digest: {result.note.packet_digest}")


@app.command()
def suite(
    suite_config: Annotated[Path, typer.Argument(help="JSON Data Access suite configuration.")],
    output: Annotated[Path, typer.Argument(help="New exclusive suite output directory.")],
) -> None:
    """Prepare and run frozen Data Access cases from a suite config."""
    if not os.environ.get("OPENAI_API_KEY"):
        _abort("OPENAI_API_KEY is required for live Data Access runs")
    try:
        from .execution import OpenAIResponsesClient
        from .suite import SuiteConfig, run_suite

        config = _load_contract(suite_config, SuiteConfig)
        run_suite(config, output, client=OpenAIResponsesClient())
    except (OSError, ValidationError, ValueError, TypeError) as error:
        _abort(f"could not run Data Access suite: {error}")
    typer.echo(f"Data Access suite complete: {output}")


@app.command()
def uptake(
    run_root: Annotated[Path, typer.Argument(help="Private Data Access run directory.")],
    output: Annotated[Path, typer.Argument(help="New exclusive uptake report directory.")],
) -> None:
    """Write post-reveal uptake diagnostics for completed arms."""
    try:
        from .uptake import UptakeReport, evaluate_run_uptake, write_uptake_report

        write_uptake_report(UptakeReport(records=evaluate_run_uptake(run_root)), output)
    except FileExistsError:
        _abort(f"output directory already exists: {output}")
    except (OSError, ValidationError, ValueError, TypeError) as error:
        if str(error) == "committed packet is not a DSX Packet":
            _abort("committed packet is not a DSX Packet")
        _abort(f"could not evaluate Data Access uptake: {error}")
    typer.echo(f"Wrote Data Access uptake: {output / 'uptake.json'}")
