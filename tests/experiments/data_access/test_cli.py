from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import typer
from pydantic import ValidationError
from pytest import MonkeyPatch
from typer.testing import CliRunner

from dsx.experiments.data_access import cli
from dsx.experiments.data_access.canonical import canonical_digest
from dsx.experiments.data_access.cli import (
    DATABASE_FILENAME,
    MANIFEST_FILENAME,
    DataAccessRunManifest,
    _load_manifest,
    _validate_prepared_input,
    app,
)
from dsx.experiments.data_access.models import DataAccessManifest

runner = CliRunner()


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    dataset = tmp_path / "data.csv"
    dataset.write_text("label,value\n0,one\n1,two\n", encoding="utf-8")
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "case_id": "case",
                "task_prompt": "inspect it",
                "dataset_path": str(dataset),
                "dataset_format": "csv",
                "target_column": "label",
            }
        ),
        encoding="utf-8",
    )
    packet = tmp_path / "packet.json"
    packet.write_text('{"future":{"trace":[1]}}', encoding="utf-8")
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "input": {"usd_per_million_tokens": 1.0},
                "output": {"usd_per_million_tokens": 2.0},
                "source": "test",
                "effective_date": "2026-08-26",
            }
        ),
        encoding="utf-8",
    )
    return case, packet, pricing


def test_cli_help_describes_data_access_v2_three_arm_repetitions() -> None:
    root_help = runner.invoke(app, ["--help"])
    prepare_help = runner.invoke(app, ["prepare", "--help"])
    run_help = runner.invoke(app, ["run", "--help"])
    assert root_help.exit_code == 0
    assert "v2 three-arm" in root_help.output
    assert prepare_help.exit_code == 0
    assert "Three-arm repetitions" in prepare_help.output
    assert run_help.exit_code == 0
    assert "Three-arm order randomization seed" in run_help.output


def test_prepare_writes_committed_input_bundle(tmp_path: Path) -> None:
    case, packet, pricing = _write_inputs(tmp_path)
    output = tmp_path / "prepared"
    result = runner.invoke(
        app,
        [
            "prepare",
            str(case),
            str(packet),
            str(output),
            "--model",
            "offline",
            "--pricing",
            str(pricing),
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = DataAccessManifest.model_validate_json((output / MANIFEST_FILENAME).read_text())
    assert (output / DATABASE_FILENAME).is_file()
    assert manifest.packet.value == {"future": {"trace": [1]}}


def test_prepare_keeps_destinations_exclusive_and_rejects_bad_input(tmp_path: Path) -> None:
    case, packet, pricing = _write_inputs(tmp_path)
    output = tmp_path / "prepared"
    arguments = [
        "prepare",
        str(case),
        str(packet),
        str(output),
        "--model",
        "offline",
        "--pricing",
        str(pricing),
    ]
    assert runner.invoke(app, arguments).exit_code == 0
    duplicate = runner.invoke(app, arguments)
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.output
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    failed = runner.invoke(
        app,
        [
            "prepare",
            str(case),
            str(bad),
            str(tmp_path / "bad-output"),
            "--model",
            "offline",
            "--pricing",
            str(pricing),
        ],
    )
    assert failed.exit_code == 1
    assert "could not prepare" in failed.output


def test_run_requires_api_key_before_creating_a_run_root(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["run", str(tmp_path / "missing-input"), str(tmp_path / "run"), "--order-seed", "1"],
    )
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output
    assert not (tmp_path / "run").exists()


def test_cli_helpers_env_dotenv_and_input_commitment_fail_closed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    case, packet, pricing = _write_inputs(tmp_path)
    prepared = tmp_path / "prepared"
    assert runner.invoke(
        app,
        [
            "prepare",
            str(case),
            str(packet),
            str(prepared),
            "--model",
            "offline",
            "--pricing",
            str(pricing),
        ],
    ).exit_code == 0
    manifest = _load_manifest(prepared)
    _validate_prepared_input(prepared, manifest)
    assert cli._quote_identifier('a"b') == '"a""b"'
    assert cli._sha256_file(Path(manifest.case.dataset_path)) == manifest.dataset.source_digest
    with pytest.raises(ValueError, match="input manifest"):
        _load_manifest(tmp_path / "missing")
    source = Path(manifest.case.dataset_path)
    source.write_text("label,value\n0,changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source dataset"):
        _validate_prepared_input(prepared, manifest)
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comment\nexport QUOTED='value'\nEMPTY=\nINVALID-KEY=x\n", encoding="utf-8")
    monkeypatch.delenv("QUOTED", raising=False)
    cli._load_dotenv(dotenv)
    assert __import__("os").environ["QUOTED"] == "value"
    cli._load_dotenv(tmp_path / "absent.env")
    with pytest.raises(ValueError, match="input digest"):
        DataAccessRunManifest(
            run_manifest_version="data-access-run-manifest-v2",
            input_manifest=manifest,
            input_manifest_digest="0" * 64,
            order_seed=1,
        )


def test_run_manifest_version_is_explicit_and_fail_closed(tmp_path: Path) -> None:
    case, packet, pricing = _write_inputs(tmp_path)
    prepared = tmp_path / "prepared"
    assert runner.invoke(
        app,
        [
            "prepare",
            str(case),
            str(packet),
            str(prepared),
            "--model",
            "offline",
            "--pricing",
            str(pricing),
        ],
    ).exit_code == 0
    manifest = _load_manifest(prepared)
    values = {
        "run_manifest_version": "data-access-run-manifest-v2",
        "input_manifest": manifest,
        "input_manifest_digest": canonical_digest(manifest),
        "order_seed": 1,
    }
    assert DataAccessRunManifest.model_validate(values).run_manifest_version == (
        "data-access-run-manifest-v2"
    )
    for version in (None, "data-access-run-manifest-v1"):
        artifact = dict(values)
        if version is None:
            del artifact["run_manifest_version"]
        else:
            artifact["run_manifest_version"] = version
        with pytest.raises(ValidationError, match="run_manifest_version"):
            DataAccessRunManifest.model_validate(artifact)


def test_run_judge_and_reveal_cli_success_and_error_paths(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    case, packet, pricing = _write_inputs(tmp_path)
    prepared = tmp_path / "prepared"
    assert runner.invoke(
        app,
        [
            "prepare",
            str(case),
            str(packet),
            str(prepared),
            "--model",
            "offline",
            "--pricing",
            str(pricing),
        ],
    ).exit_code == 0
    from dsx.experiments.data_access import blind, execution

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls: list[object] = []
    monkeypatch.setattr(execution, "OpenAIResponsesClient", lambda: "client")
    monkeypatch.setattr(execution, "run_experiment", lambda **kwargs: calls.append(kwargs))
    run_root = tmp_path / "run"
    completed = runner.invoke(
        app, ["run", str(prepared), str(run_root), "--order-seed", "3"]
    )
    assert completed.exit_code == 0, completed.output
    assert calls and (run_root / "run_manifest.json").is_file()

    monkeypatch.setattr(
        blind,
        "export_blind",
        lambda *_args, **_kwargs: SimpleNamespace(eligible_count=2),
    )
    exported_result = runner.invoke(
        app, ["judge", str(run_root), str(tmp_path / "blind"), "--blind-seed", "2"]
    )
    assert exported_result.exit_code == 0
    blind_bundle = tmp_path / "blind"
    blind_bundle.mkdir()
    (blind_bundle / "manifest.json").write_text(
        blind.BlindManifest(
            version="data-access-blind-v2",
            blind_seed=2, ordered_opaque_ids=(), eligible_count=0, output_digests=()
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        blind,
        "freeze_judgments",
        lambda *_args, **_kwargs: SimpleNamespace(judgments_digest="a" * 64),
    )
    judgments = tmp_path / "judgments.json"
    judgments.write_text("[]", encoding="utf-8")
    frozen_result = runner.invoke(
        app,
        [
            "judge",
            str(run_root),
            str(blind_bundle),
            "--blind-seed",
            "2",
            "--judgments",
            str(judgments),
        ],
    )
    assert frozen_result.exit_code == 0
    wrong_seed = runner.invoke(
        app,
        [
            "judge",
            str(run_root),
            str(blind_bundle),
            "--blind-seed",
            "3",
            "--judgments",
            str(judgments),
        ],
    )
    assert wrong_seed.exit_code == 1
    assert "blind seed" in wrong_seed.output
    judgments.write_text("{}", encoding="utf-8")
    invalid_judgments = runner.invoke(
        app,
        [
            "judge",
            str(run_root),
            str(blind_bundle),
            "--blind-seed",
            "2",
            "--judgments",
            str(judgments),
        ],
    )
    assert invalid_judgments.exit_code == 1
    monkeypatch.setattr(
        blind,
        "reveal_blind",
        lambda *_args: (object(), SimpleNamespace(raw_judgments=(1,))),
    )
    revealed = runner.invoke(app, ["reveal", str(run_root), str(tmp_path / "blind")])
    assert revealed.exit_code == 0
    monkeypatch.setattr(
        blind,
        "reveal_blind",
        lambda *_args: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert runner.invoke(app, ["reveal", str(run_root), str(tmp_path / "blind")]).exit_code == 1


def test_cli_failure_boundaries_cover_directory_and_dataset_tampering(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    case, packet, pricing = _write_inputs(tmp_path)
    prepared = tmp_path / "prepared"
    assert runner.invoke(
        app,
        [
            "prepare",
            str(case),
            str(packet),
            str(prepared),
            "--model",
            "offline",
            "--pricing",
            str(pricing),
        ],
    ).exit_code == 0
    manifest = _load_manifest(prepared)
    wrong_path = manifest.model_copy(
        update={"dataset": manifest.dataset.model_copy(update={"database_path": "other.duckdb"})}
    )
    with pytest.raises(ValueError, match="database path"):
        _validate_prepared_input(prepared, wrong_path)
    database = prepared / DATABASE_FILENAME
    connection = duckdb.connect(str(database))
    connection.execute("UPDATE dataset SET value = 'tampered' WHERE label = 0")
    connection.close()
    with pytest.raises(ValueError, match="materialized"):
        _validate_prepared_input(prepared, manifest)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        cli,
        "_validate_prepared_input",
        lambda *_args: (_ for _ in ()).throw(ValueError("bad")),
    )
    failed = runner.invoke(
        app, ["run", str(prepared), str(tmp_path / "run"), "--order-seed", "1"]
    )
    assert failed.exit_code == 1

    def denied_mkdir(self: Path, **_kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr(Path, "mkdir", denied_mkdir)
    with pytest.raises(typer.Exit):
        cli._create_directory(tmp_path / "denied", "destination")
