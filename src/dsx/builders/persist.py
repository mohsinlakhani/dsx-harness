"""Exclusive atomic persistence for immutable packet bundles."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from dsx.packet.models import DsxPacket
from dsx.pipeline import TransformationManifest

from .models import PacketBuildRecord, PacketBuildResult


def write_packet_bundle(result: PacketBuildResult, output: Path) -> None:
    """Write a packet bundle to a new directory. Never overwrites an existing path."""
    parent = output.parent
    if not parent.exists():
        raise ValueError(f"output parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise ValueError(f"output parent is not a directory: {parent}")
    try:
        output.mkdir()
    except FileExistsError as error:
        raise FileExistsError(f"output destination already exists: {output}") from error
    reserved = True
    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=parent))
        _write_bundle_contents(result, temporary)
        _validate_bundle(temporary, result)
        # Brief window after rmdir: POSIX rename can replace an empty dest. Exclusive
        # mkdir above is the reservation; this rmdir is required for the atomic rename.
        output.rmdir()
        reserved = False
        temporary.rename(output)
    except Exception:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if reserved:
            try:
                output.rmdir()
            except OSError:
                pass
        raise


def _write_bundle_contents(result: PacketBuildResult, directory: Path) -> None:
    (directory / "packet.json").write_text(
        result.packet.canonical_json() + "\n", encoding="utf-8"
    )
    (directory / "build-record.json").write_text(
        result.build_record.canonical_json() + "\n", encoding="utf-8"
    )
    if result.manifest is not None:
        (directory / "manifest.json").write_text(
            result.manifest.canonical_json() + "\n", encoding="utf-8"
        )


def _validate_bundle(directory: Path, result: PacketBuildResult) -> None:
    packet = DsxPacket.model_validate_json(
        (directory / "packet.json").read_text(encoding="utf-8")
    )
    record = PacketBuildRecord.model_validate_json(
        (directory / "build-record.json").read_text(encoding="utf-8")
    )
    if packet.digest() != result.packet.digest():
        raise ValueError("written packet digest does not match the build result")
    if record.packet_digest != packet.digest():
        raise ValueError("build record packet digest does not match the written packet")
    if result.manifest is not None:
        restored = TransformationManifest.model_validate_json(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        if restored != result.manifest:
            raise ValueError("written manifest does not match the normalized input")
        written = restored.digest()
        expected_digest = result.manifest.digest()
        if written != expected_digest:  # pragma: no cover - inequality is caught above
            raise ValueError("written manifest digest does not match the build result")
    expected = {"packet.json", "build-record.json"}
    if result.manifest is not None:
        expected.add("manifest.json")
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise ValueError("bundle contains unexpected files")
