"""Tests for exclusive bundle persistence and previous-bundle validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from dsx.builders import PacketBuildRequest, build_packet, write_packet_bundle
from dsx.builders.build import load_previous_bundle
from dsx.builders.models import PacketBuildRecord
from dsx.builders.persist import _validate_bundle
from dsx.packet.models import DsxPacket
from dsx.pipeline import (
    DatasetFormat,
    SnapshotRole,
    TransformationManifest,
    TransformationOperation,
)
from tests.builders.helpers import digest_of, manifest, snapshot, step, write_csv


def _result(tmp_path: Path, with_manifest: bool = False):
    dataset = write_csv(
        tmp_path / "current.csv", [{"label": "pos", "x": 1}, {"label": "neg", "x": 2}]
    )
    declared = None
    if with_manifest:
        declared = manifest(
            current="current",
            snapshots=(
                snapshot(
                    "current",
                    digest_of(dataset),
                    SnapshotRole.source,
                    path="current.csv",
                    fmt=DatasetFormat.csv,
                ),
            ),
        )
    return build_packet(
        PacketBuildRequest(
            dataset_path=dataset,
            target_column="label",
            packet_id="case-v1",
            manifest=declared,
            snapshot_root=tmp_path if with_manifest else None,
        )
    )


def test_writes_expected_files_and_refuses_existing_output(tmp_path: Path) -> None:
    without_manifest = _result(tmp_path)
    output = tmp_path / "bundle"
    write_packet_bundle(without_manifest, output)
    assert {path.name for path in output.iterdir()} == {"packet.json", "build-record.json"}
    packet = DsxPacket.model_validate_json((output / "packet.json").read_text(encoding="utf-8"))
    record = PacketBuildRecord.model_validate_json(
        (output / "build-record.json").read_text(encoding="utf-8")
    )
    assert packet.digest() == record.packet_digest == without_manifest.packet.digest()
    assert not any(path.suffix in {".csv", ".parquet"} for path in output.iterdir())
    original = (output / "packet.json").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        write_packet_bundle(without_manifest, output)
    assert (output / "packet.json").read_text(encoding="utf-8") == original

    nested = tmp_path / "nested"
    nested.mkdir()
    with_manifest = _result(nested, with_manifest=True)
    manifest_output = tmp_path / "with-manifest"
    write_packet_bundle(with_manifest, manifest_output)
    restored = TransformationManifest.model_validate_json(
        (manifest_output / "manifest.json").read_text(encoding="utf-8")
    )
    assert restored == with_manifest.manifest
    assert restored.snapshots[0].path == "current.csv"


def test_cleans_temporary_directory_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result(tmp_path)
    output = tmp_path / "bundle"

    def fail_rename(self: Path, target: Path) -> Path:
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(OSError, match="rename failed"):
        write_packet_bundle(result, output)
    assert not output.exists()
    assert list(tmp_path.glob(".bundle.tmp-*")) == []


def test_previous_bundle_validation(tmp_path: Path) -> None:
    result = _result(tmp_path)
    output = tmp_path / "previous"
    write_packet_bundle(result, output)
    packet, record = load_previous_bundle(output)
    assert packet.digest() == record.packet_digest
    with pytest.raises(ValueError, match="does not exist"):
        load_previous_bundle(tmp_path / "missing")
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "packet.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        load_previous_bundle(incomplete)
    (incomplete / "build-record.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be parsed"):
        load_previous_bundle(incomplete)
    tampered = tmp_path / "tampered"
    write_packet_bundle(result, tampered)
    record_path = tampered / "build-record.json"
    parsed = PacketBuildRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
    broken = parsed.model_copy(update={"packet_digest": "f" * 64})
    record_path.write_text(broken.canonical_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="packet digest does not match"):
        load_previous_bundle(tampered)


def test_write_validates_parent_and_reparsed_artifacts(tmp_path: Path) -> None:
    result = _result(tmp_path)
    with pytest.raises(ValueError, match="parent directory does not exist"):
        write_packet_bundle(result, tmp_path / "missing-parent" / "bundle")
    parent_file = tmp_path / "as-file"
    parent_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        write_packet_bundle(result, parent_file / "bundle")

    output = tmp_path / "ok"
    write_packet_bundle(result, output)
    _validate_bundle(output, result)
    (output / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        _validate_bundle(output, result)
    (output / "extra.json").unlink()
    packet_path = output / "packet.json"
    original_packet = packet_path.read_text(encoding="utf-8")
    packet_path.write_text(original_packet.replace(result.packet.packet_id, "other-id"), encoding="utf-8")
    with pytest.raises(ValueError, match="written packet digest"):
        _validate_bundle(output, result)
    packet_path.write_text(original_packet, encoding="utf-8")
    record_path = output / "build-record.json"
    parsed = PacketBuildRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
    record_path.write_text(
        parsed.model_copy(update={"packet_digest": "e" * 64}).canonical_json() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="build record packet digest"):
        _validate_bundle(output, result)


def test_validate_bundle_rejects_rewritten_manifest(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    result = _result(src, with_manifest=True)
    output = tmp_path / "bundle"
    write_packet_bundle(result, output)
    manifest_path = output / "manifest.json"
    restored = TransformationManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    rewritten = restored.model_copy(update={"pipeline_id": "other-pipeline"})
    manifest_path.write_text(rewritten.canonical_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="written manifest does not match"):
        _validate_bundle(output, result)


def test_relative_historical_path_resolves_from_snapshot_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    raw = write_csv(data_dir / "raw.csv", [{"label": "neg"}, {"label": "pos"}])
    current = write_csv(data_dir / "train.csv", [{"label": "neg"}, {"label": "pos"}])
    declared = manifest(
        current="train",
        snapshots=(
            snapshot(
                "raw",
                digest_of(raw),
                SnapshotRole.source,
                path="data/raw.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "train",
                digest_of(current),
                SnapshotRole.train,
                path="data/train.csv",
                fmt=DatasetFormat.csv,
            ),
        ),
        steps=(step("copy", TransformationOperation.filter, ("raw",), ("train",)),),
    )
    result = build_packet(
        PacketBuildRequest(
            dataset_path=current,
            target_column="label",
            packet_id="rel-v1",
            manifest=declared,
            snapshot_root=tmp_path,
        )
    )
    history = result.packet.modules[2].content
    assert isinstance(history, dict)
    assert "raw" in history["accessible_snapshot_ids"]
