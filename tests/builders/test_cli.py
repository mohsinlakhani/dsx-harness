"""CLI tests for dsx-packet build."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from dsx.builders.cli import app
from dsx.pipeline import DatasetFormat, SnapshotRole, TransformationOperation
from tests.builders.helpers import digest_of, manifest, snapshot, step, write_csv, write_parquet

runner = CliRunner()


def test_cli_builds_csv_and_parquet_end_to_end(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "data.csv", [{"label": "pos"}, {"label": "neg"}])
    parquet_path = write_parquet(tmp_path / "data.parquet", [{"label": "pos"}, {"label": "neg"}])
    csv_out = tmp_path / "csv-bundle"
    parquet_out = tmp_path / "parquet-bundle"
    csv_result = runner.invoke(
        app,
        [
            "build",
            str(csv_path),
            str(csv_out),
            "--target",
            "label",
            "--packet-id",
            "csv-v1",
        ],
    )
    parquet_result = runner.invoke(
        app,
        [
            "build",
            str(parquet_path),
            str(parquet_out),
            "--target",
            "label",
            "--packet-id",
            "parquet-v1",
        ],
    )
    assert csv_result.exit_code == 0, csv_result.output
    assert parquet_result.exit_code == 0, parquet_result.output
    assert "packet_digest=" in csv_result.output
    assert "module_ids=dataset-profile,target-profile,data-traps" in csv_result.output
    assert (csv_out / "packet.json").is_file()
    assert (parquet_out / "packet.json").is_file()


def test_cli_manifest_previous_bundle_and_error_categories(tmp_path: Path) -> None:
    current = write_csv(tmp_path / "train.csv", [{"label": "neg"}] * 5 + [{"label": "pos"}] * 5)
    raw = write_csv(tmp_path / "raw.csv", [{"label": "neg"}] * 9 + [{"label": "pos"}])
    declared = manifest(
        current="train",
        snapshots=(
            snapshot(
                "raw",
                digest_of(raw),
                SnapshotRole.source,
                path="raw.csv",
                fmt=DatasetFormat.csv,
            ),
            snapshot(
                "train",
                digest_of(current),
                SnapshotRole.train,
                path="train.csv",
                fmt=DatasetFormat.csv,
            ),
        ),
        steps=(step("prep", TransformationOperation.filter, ("raw",), ("train",)),),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(declared.canonical_json(), encoding="utf-8")
    first_out = tmp_path / "first"
    first = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(first_out),
            "--target",
            "label",
            "--packet-id",
            "train-v1",
            "--manifest",
            str(manifest_path),
        ],
    )
    assert first.exit_code == 0, first.output
    assert "revision=1" in first.output
    second_out = tmp_path / "second"
    second = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(second_out),
            "--target",
            "label",
            "--packet-id",
            "train-v2",
            "--manifest",
            str(manifest_path),
            "--previous-bundle",
            str(first_out),
        ],
    )
    assert second.exit_code == 0, second.output
    assert "revision=2" in second.output

    existing = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(first_out),
            "--target",
            "label",
            "--packet-id",
            "train-v1",
        ],
    )
    assert existing.exit_code == 1
    assert "already exists" in existing.output

    missing_dataset = runner.invoke(
        app,
        [
            "build",
            str(tmp_path / "missing.csv"),
            str(tmp_path / "out-missing"),
            "--target",
            "label",
            "--packet-id",
            "train-v1",
        ],
    )
    assert missing_dataset.exit_code == 1
    assert "does not exist" in missing_dataset.output

    missing_target = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(tmp_path / "out-target"),
            "--target",
            "absent",
            "--packet-id",
            "train-v1",
        ],
    )
    assert missing_target.exit_code == 1
    assert "target column" in missing_target.output

    bad_id = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(tmp_path / "out-id"),
            "--target",
            "label",
            "--packet-id",
            "bad id",
        ],
    )
    assert bad_id.exit_code == 1

    missing_manifest = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(tmp_path / "out-man"),
            "--target",
            "label",
            "--packet-id",
            "train-v1",
            "--manifest",
            str(tmp_path / "no-manifest.json"),
        ],
    )
    assert missing_manifest.exit_code == 1
    assert "manifest does not exist" in missing_manifest.output

    bad_manifest = tmp_path / "bad.json"
    bad_manifest.write_text("{", encoding="utf-8")
    invalid_manifest = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(tmp_path / "out-badman"),
            "--target",
            "label",
            "--packet-id",
            "train-v1",
            "--manifest",
            str(bad_manifest),
        ],
    )
    assert invalid_manifest.exit_code == 1
    assert "manifest is invalid" in invalid_manifest.output

    invalid_graph = tmp_path / "bad-graph.json"
    invalid_graph.write_text(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("train", "b" * 64, SnapshotRole.train),
            ),
        ).canonical_json(),
        encoding="utf-8",
    )
    graph_error = runner.invoke(
        app,
        [
            "build",
            str(current),
            str(tmp_path / "out-graph"),
            "--target",
            "label",
            "--packet-id",
            "train-v1",
            "--manifest",
            str(invalid_graph),
        ],
    )
    assert graph_error.exit_code == 1
    assert "Error:" in graph_error.output
