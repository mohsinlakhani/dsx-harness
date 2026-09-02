"""Command-line boundary for building immutable DSX Packet bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from dsx.pipeline import TransformationManifest

from .build import build_packet, load_previous_bundle
from .models import PacketBuildRequest
from .persist import write_packet_bundle

app = typer.Typer(
    add_completion=False,
    help="Build an immutable history-aware DSX Packet bundle.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Build an immutable history-aware DSX Packet bundle."""


def _abort(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    detail = str(first.get("msg", error))
    if location:
        return f"{location}: {detail}"
    return detail


@app.command("build")
def build_command(
    dataset_path: Annotated[
        Path,
        typer.Argument(help="Current CSV or Parquet dataset. The file is not copied."),
    ],
    output: Annotated[
        Path,
        typer.Argument(help="New exclusive directory for the immutable packet bundle."),
    ],
    target: Annotated[
        str,
        typer.Option("--target", help="Target column in the current dataset."),
    ],
    packet_id: Annotated[
        str,
        typer.Option("--packet-id", help="Packet identity satisfying PacketId."),
    ],
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Optional DSX transformation manifest JSON."),
    ] = None,
    previous_bundle: Annotated[
        Path | None,
        typer.Option("--previous-bundle", help="Optional previous packet bundle directory."),
    ] = None,
) -> None:
    """Build a DSX Packet from a dataset and optional transformation history."""
    try:
        parsed_manifest: TransformationManifest | None = None
        snapshot_root: Path | None = None
        if manifest is not None:
            if not manifest.is_file():
                _abort(f"manifest does not exist: {manifest}")
            try:
                parsed_manifest = TransformationManifest.model_validate_json(
                    manifest.read_text(encoding="utf-8")
                )
            except ValidationError as error:
                _abort(f"manifest is invalid: {_validation_message(error)}")
            snapshot_root = manifest.parent
        previous_record = None
        previous_digest = None
        if previous_bundle is not None:
            previous_packet, previous_record = load_previous_bundle(previous_bundle)
            previous_digest = previous_packet.digest()
        request = PacketBuildRequest(
            dataset_path=dataset_path,
            target_column=target,
            packet_id=packet_id,
            manifest=parsed_manifest,
            snapshot_root=snapshot_root,
            previous_build_record=previous_record,
            previous_packet_digest=previous_digest,
        )
        result = build_packet(request)
        write_packet_bundle(result, output)
    except typer.Exit:
        raise
    except FileExistsError as error:
        _abort(str(error))
    except ValidationError as error:
        _abort(_validation_message(error))
    except (ValueError, OSError) as error:
        _abort(str(error))

    typer.echo(str(output))
    typer.echo(f"packet_digest={result.packet.digest()}")
    typer.echo(f"dataset_digest={result.build_record.dataset_digest}")
    typer.echo(f"revision={result.build_record.revision}")
    typer.echo(
        "module_ids=" + ",".join(module.module_id for module in result.packet.modules)
    )


if __name__ == "__main__":  # pragma: no cover
    app()
