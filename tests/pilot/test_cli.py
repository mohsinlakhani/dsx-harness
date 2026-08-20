"""End-to-end tests for the public proof-first pilot CLI."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from dsx.pilot import cli
from dsx.pilot.blind import (
    FROZEN_JUDGMENTS_FILENAME,
    MANIFEST_FILENAME,
    REVEAL_DIRECTORY,
    REVEAL_MAP_FILENAME,
    REVEALED_REPORT_FILENAME,
)
from dsx.pilot.models import (
    AnalysisDecision,
    BlindJudgment,
    BlindManifest,
    FrozenJudgments,
    Metric,
    PilotCase,
    RunManifest,
)
from dsx.pilot.render import RenderedRequests, RequestConfiguration
from dsx.pilot.runner import ModelReply, ModelTransportError, ScriptedModelClient


def _decision() -> AnalysisDecision:
    return AnalysisDecision(
        primary_metric=Metric.recall_at_5_percent,
        supporting_metrics=(Metric.precision_at_5_percent,),
        review_budget_fraction=0.05,
        split_strategy="stratified validation",
        excluded_columns=("row_id",),
        reasoning="Rank within the fixed five-percent review budget.",
        limitations=("One synthetic case",),
        recommendation="Use a ranking model.",
        packet_citations=(),
    )


def _generate(runner: CliRunner, destination: Path, *options: str) -> None:
    result = runner.invoke(
        cli.app,
        ["generate", str(destination), "--model", "gpt-test", *options],
    )
    assert result.exit_code == 0, result.output


def test_load_dotenv_and_generate_uses_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli._load_dotenv(tmp_path / "missing.env")
    cli._load_dotenv(tmp_path)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "# comment\n"
        "not-an-assignment\n"
        "=missing-key\n"
        "invalid-key=value\n"
        "export MODEL_ID='dotenv-model'\n"
        'OPENAI_API_KEY="dotenv-key"\n'
        "EMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cli._load_dotenv(dotenv_path)
    assert os.environ["MODEL_ID"] == "dotenv-model"
    assert os.environ["OPENAI_API_KEY"] == "dotenv-key"
    assert "EMPTY" not in os.environ

    monkeypatch.setenv("MODEL_ID", "shell-model")
    cli._load_dotenv(dotenv_path)
    assert os.environ["MODEL_ID"] == "shell-model"

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_ID")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["generate", "generated"])

    assert result.exit_code == 0, result.output
    configuration = RequestConfiguration.model_validate_json(
        (tmp_path / "generated" / "request_configuration.json").read_text(encoding="utf-8")
    )
    assert configuration.model_identifier == "dotenv-model"

    dotenv_path.unlink()
    monkeypatch.delenv("MODEL_ID")
    result = runner.invoke(cli.app, ["generate", "missing-model"])
    assert result.exit_code != 0
    assert "model identifier is required" in result.output


def _run(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    generated: Path,
    run_root: Path,
    client: ScriptedModelClient,
    *,
    order_seed: int = 17,
) -> Result:
    monkeypatch.setattr(cli, "OpenAIModelClient", lambda: client)
    return runner.invoke(
        cli.app,
        ["run", str(generated), str(run_root), "--order-seed", str(order_seed)],
        env={"OPENAI_API_KEY": "test-secret-never-print"},
    )


def _judgments(opaque_ids: tuple[str, ...]) -> list[BlindJudgment]:
    return [
        BlindJudgment(
            opaque_id=opaque_id,
            decision_quality=4,
            evidence_use=3,
            limitations_quality=3,
            packet_guess="packet_on",
            guess_confidence=2,
            metric_reasoning=4,
            split_strategy=4,
            leakage_row_id_avoidance=5,
            limitations=3,
            overall_recommendation_quality=4,
            exact_prevalence_recognition=3,
            majority_baseline_recognition=3,
            citation_use=2,
        )
        for opaque_id in opaque_ids
    ]


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _tamper_blind_manifest(
    bundle: Path, mutation: str
) -> BlindManifest:
    """Make a typed, self-consistent public-manifest change not backed by the source run."""
    manifest_path = bundle / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text())
    mutations = {
        "version": lambda: payload.update(version="blind-v2"),
        "ordered IDs": lambda: (
            payload["ordered_opaque_ids"].reverse(),
            payload["output_digests"].reverse(),
        ),
        "exclusions": lambda: payload.update(
            exclusions=[{"reason": "fabricated_exclusion", "count": 1}]
        ),
    }
    mutations[mutation]()
    manifest_path.write_text(json.dumps(payload))
    return BlindManifest.model_validate_json(manifest_path.read_text())


def _cohere_frozen_manifest_digest(bundle: Path, manifest: BlindManifest) -> None:
    """Model a coordinated post-freeze edit that preserves frozen internal digests."""
    frozen_path = bundle / FROZEN_JUDGMENTS_FILENAME
    frozen = FrozenJudgments.model_validate_json(frozen_path.read_text())
    by_id = {judgment.opaque_id: judgment for judgment in frozen.judgments}
    judgments = tuple(by_id[opaque_id] for opaque_id in manifest.ordered_opaque_ids)
    frozen_path.write_text(
        frozen.model_copy(
            update={
                "manifest_digest": _canonical_digest(manifest.model_dump(mode="json")),
                "judgments": judgments,
                "judgments_digest": _canonical_digest(
                    [judgment.model_dump(mode="json") for judgment in judgments]
                ),
            }
        ).model_dump_json()
    )


def test_app_and_each_of_exactly_four_commands_have_help() -> None:
    """Dropping or silently adding a public command breaks the documented CLI surface."""
    runner = CliRunner()

    root = runner.invoke(cli.app, ["--help"])

    assert root.exit_code == 0
    assert "generate" in root.output
    assert "run" in root.output
    assert "judge" in root.output
    assert "reveal" in root.output
    registered = {command.name for command in cli.app.registered_commands}
    assert registered == {"generate", "run", "judge", "reveal"}
    for command in sorted(registered):
        result = runner.invoke(cli.app, [command, "--help"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output


def test_generate_writes_only_typed_human_readable_artifacts_and_never_overwrites(
    tmp_path: Path,
) -> None:
    """A generation run must commit complete typed inputs without dumping 5,000 rows."""
    runner = CliRunner()
    destination = tmp_path / "generated"

    result = runner.invoke(
        cli.app,
        [
            "generate",
            str(destination),
            "--model",
            "gpt-test",
            "--seed",
            "7",
            "--system-prompt",
            "Return typed analysis.",
            "--response-schema-name",
            "custom_decision",
        ],
    )

    assert result.exit_code == 0, result.output
    assert {path.name for path in destination.iterdir()} == {
        "case.json",
        "packet.json",
        "request_configuration.json",
        "rendered_requests.json",
    }
    case = PilotCase.model_validate_json((destination / "case.json").read_text())
    configuration = RequestConfiguration.model_validate_json(
        (destination / "request_configuration.json").read_text()
    )
    rendered = RenderedRequests.model_validate_json(
        (destination / "rendered_requests.json").read_text()
    )
    assert case.generation_seed == 7
    assert configuration.system_prompt == "Return typed analysis."
    assert configuration.response_schema_name == "custom_decision"
    assert rendered.packet_on.request.model_identifier == "gpt-test"
    assert "case digest:" in result.output.lower()
    assert "request digests:" in result.output.lower()
    assert "case-04999" not in result.output
    assert '\n  "generation_seed"' in (destination / "case.json").read_text()

    previous = (destination / "case.json").read_bytes()
    repeated = runner.invoke(
        cli.app,
        ["generate", str(destination), "--model", "other-model"],
    )
    assert repeated.exit_code != 0
    assert "already exists" in repeated.output.lower()
    assert "Traceback" not in repeated.output
    assert (destination / "case.json").read_bytes() == previous


def test_generate_verifies_the_default_frozen_digest_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-fixture drift must abort before publishing a generated directory."""
    monkeypatch.setattr(cli, "FROZEN_CASE_DIGEST", "0" * 64)
    destination = tmp_path / "generated"

    result = CliRunner().invoke(
        cli.app,
        ["generate", str(destination), "--model", "gpt-test"],
    )

    assert result.exit_code != 0
    assert "frozen case digest" in result.output.lower()
    assert not destination.exists()
    assert "Traceback" not in result.output


@pytest.mark.parametrize("schema_name", ["bad name!", "a" * 65])
def test_generate_rejects_schema_names_the_provider_cannot_accept(
    tmp_path: Path, schema_name: str
) -> None:
    """Invalid wire schema names must fail offline instead of consuming live attempts."""
    destination = tmp_path / "generated"

    result = CliRunner().invoke(
        cli.app,
        [
            "generate",
            str(destination),
            "--model",
            "gpt-test",
            "--response-schema-name",
            schema_name,
        ],
    )

    assert result.exit_code != 0
    assert "invalid request configuration" in result.output.lower()
    assert not destination.exists()
    assert "Traceback" not in result.output


def test_run_reloads_verified_inputs_writes_manifest_then_runs_three_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing intended pair count, deterministic seeds, or live-boundary rendering fails."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    _generate(runner, generated)
    client = ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6)

    result = _run(runner, monkeypatch, generated, run_root, client, order_seed=17)

    assert result.exit_code == 0, result.output
    manifest = RunManifest.model_validate_json((run_root / "run_manifest.json").read_text())
    assert manifest.intended_pair_count == 3
    assert manifest.base_order_seed == 17
    assert manifest.model_identifier == "gpt-test"
    assert manifest.claim_label == "one-case unscored information-availability pilot"
    expected_pair_ids = tuple(
        hashlib.sha256(
            f"dsx-pilot:v1:{manifest.case_digest}:17:{number}".encode()
        ).hexdigest()[:16]
        for number in range(1, 4)
    )
    expected_order_seeds = tuple(
        int.from_bytes(
            hashlib.sha256(f"dsx-pilot:order:v1:17:{number}".encode()).digest()[:8],
            "big",
        )
        for number in range(1, 4)
    )
    assert manifest.pair_ids == expected_pair_ids
    assert manifest.pair_order_seeds == expected_order_seeds
    assert len(client.requests) == 6
    assert len(list(run_root.glob("pair-*/pair_summary.json"))) == 3
    attempt_ids = [
        json.loads(path.read_text())["attempt_id"]
        for path in run_root.glob("pair-*/attempts/*/attempt_start.json")
    ]
    assert len(attempt_ids) == len(set(attempt_ids)) == 3
    assert all(len(attempt_id) >= 20 for attempt_id in attempt_ids)
    assert result.output.lower().count("complete") == 3
    assert "test-secret-never-print" not in result.output

    repeated = _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    )
    assert repeated.exit_code != 0
    assert "already exists" in repeated.output.lower()
    assert "Traceback" not in repeated.output


def test_run_continues_after_infra_incomplete_and_prints_every_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping after one exhausted pair would omit intended terminal summaries."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    _generate(runner, generated, "--seed", "7")
    script = [ModelTransportError("offline")] * 6
    script.extend([ModelReply(parsed_decision=_decision())] * 4)

    result = _run(
        runner,
        monkeypatch,
        generated,
        tmp_path / "run",
        ScriptedModelClient(script),
    )

    assert result.exit_code == 0, result.output
    assert "pair 1" in result.output.lower()
    assert "infra_incomplete" in result.output
    assert "pair 2" in result.output.lower()
    assert "pair 3" in result.output.lower()
    assert result.output.lower().count("complete") == 3


def test_run_rejects_missing_key_and_tampered_or_malformed_generated_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid inputs and missing credentials must fail before live client construction."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    _generate(runner, generated, "--seed", "7")
    constructed = False

    def forbidden_client() -> ScriptedModelClient:
        nonlocal constructed
        constructed = True
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(cli, "OpenAIModelClient", forbidden_client)
    missing_key = runner.invoke(
        cli.app,
        ["run", str(generated), str(tmp_path / "missing-key"), "--order-seed", "4"],
        env={"OPENAI_API_KEY": ""},
    )
    assert missing_key.exit_code != 0
    assert "OPENAI_API_KEY" in missing_key.output
    assert not constructed
    assert not (tmp_path / "missing-key").exists()

    case_path = generated / "case.json"
    case_payload = json.loads(case_path.read_text())
    case_payload["task_text"] = "tampered task"
    case_path.write_text(json.dumps(case_payload))
    tampered = runner.invoke(
        cli.app,
        ["run", str(generated), str(tmp_path / "tampered"), "--order-seed", "4"],
        env={"OPENAI_API_KEY": "present"},
    )
    assert tampered.exit_code != 0
    assert "case digest mismatch" in tampered.output.lower()
    assert not constructed
    assert "Traceback" not in tampered.output

    case_path.write_text("{not-json")
    malformed = runner.invoke(
        cli.app,
        ["run", str(generated), str(tmp_path / "malformed"), "--order-seed", "4"],
        env={"OPENAI_API_KEY": "present"},
    )
    assert malformed.exit_code != 0
    assert "invalid generated artifact" in malformed.output.lower()
    assert not constructed
    assert "Traceback" not in malformed.output


def test_run_rejects_a_persisted_render_digest_mismatch_before_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale or modified generated render must not reach the external client seam."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    _generate(runner, generated, "--seed", "7")
    rendered_path = generated / "rendered_requests.json"
    payload = json.loads(rendered_path.read_text())
    payload["packet_off"]["request_digest"] = "0" * 64
    rendered_path.write_text(json.dumps(payload))
    monkeypatch.setattr(
        cli,
        "OpenAIModelClient",
        lambda: (_ for _ in ()).throw(AssertionError("client constructed")),
    )

    result = runner.invoke(
        cli.app,
        ["run", str(generated), str(tmp_path / "run"), "--order-seed", "4"],
        env={"OPENAI_API_KEY": "present"},
    )

    assert result.exit_code != 0
    assert "rendered request digest mismatch" in result.output.lower()
    assert not (tmp_path / "run").exists()
    assert "Traceback" not in result.output


def test_judge_freezes_complete_blind_scores_without_revealing_source_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blind export and freeze must remain usable without exposing source assignments."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    run_result = _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    )
    assert run_result.exit_code == 0

    exported = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )

    assert exported.exit_code == 0, exported.output
    manifest = BlindManifest.model_validate_json((bundle / MANIFEST_FILENAME).read_text())
    assert all(opaque_id in exported.output for opaque_id in manifest.ordered_opaque_ids)
    assert "decision_quality" in exported.output
    assert "do not give evaluators" in exported.output.lower()
    assert not any(pair_id in exported.output for pair_id in ("pair 1", "pair 2", "pair 3"))

    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                judgment.model_dump(mode="json")
                for judgment in _judgments(manifest.ordered_opaque_ids)
            ]
        )
    )
    frozen = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    )
    assert frozen.exit_code == 0, frozen.output
    assert "judgments digest:" in frozen.output.lower()
    assert str(bundle / FROZEN_JUDGMENTS_FILENAME) in frozen.output
    assert "pair-" not in frozen.output

    repeated = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    )
    assert repeated.exit_code != 0
    assert "already exists" in repeated.output.lower()
    assert "Traceback" not in repeated.output


def test_judge_rejects_bad_json_contract_and_seed_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed, partial, or differently seeded judgments must never freeze."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0

    bad_path = tmp_path / "bad.json"
    bad_path.write_text('{"not": "an array"}')
    malformed = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(bad_path),
        ],
    )
    assert malformed.exit_code != 0
    assert "invalid judgment JSON" in malformed.output
    assert "Traceback" not in malformed.output

    bad_path.write_text('[{"opaque_id": "missing-scores"}]')
    invalid = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(bad_path),
        ],
    )
    assert invalid.exit_code != 0
    assert "invalid judgment JSON" in invalid.output
    assert not (bundle / FROZEN_JUDGMENTS_FILENAME).exists()

    mismatch = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "10",
            "--judgments",
            str(bad_path),
        ],
    )
    assert mismatch.exit_code != 0
    assert "blind seed does not match" in mismatch.output.lower()


@pytest.mark.parametrize("non_integer_score", ["5", 5.0, True])
def test_judge_rejects_coercible_non_integer_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_integer_score: object,
) -> None:
    """Strings, floats, and booleans must not silently become evaluator ratings."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0
    manifest = BlindManifest.model_validate_json((bundle / MANIFEST_FILENAME).read_text())
    judgment_payloads = [
        judgment.model_dump(mode="json")
        for judgment in _judgments(manifest.ordered_opaque_ids)
    ]
    judgment_payloads[0]["decision_quality"] = non_integer_score
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(json.dumps(judgment_payloads))

    result = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    )

    assert result.exit_code != 0
    assert "invalid judgment JSON" in result.output
    assert not (bundle / FROZEN_JUDGMENTS_FILENAME).exists()
    assert "Traceback" not in result.output


def test_reveal_refuses_early_then_publishes_separate_sections_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labels must stay gated until freeze and reveal artifacts must remain immutable."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0

    early = runner.invoke(cli.app, ["reveal", str(run_root), str(bundle)])
    assert early.exit_code != 0
    assert "frozen judgments" in early.output.lower()
    assert not (bundle / REVEAL_DIRECTORY).exists()
    assert "Traceback" not in early.output

    manifest = BlindManifest.model_validate_json((bundle / MANIFEST_FILENAME).read_text())
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                judgment.model_dump(mode="json")
                for judgment in _judgments(manifest.ordered_opaque_ids)
            ]
        )
    )
    assert runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    ).exit_code == 0

    revealed = runner.invoke(cli.app, ["reveal", str(run_root), str(bundle)])

    assert revealed.exit_code == 0, revealed.output
    assert "comparative ratings:" in revealed.output.lower()
    assert "packet-uptake diagnostics:" in revealed.output.lower()
    assert "arm-guess results:" in revealed.output.lower()
    assert str(bundle / REVEAL_DIRECTORY / REVEAL_MAP_FILENAME) in revealed.output
    assert str(bundle / REVEAL_DIRECTORY / REVEALED_REPORT_FILENAME) in revealed.output
    repeated = runner.invoke(cli.app, ["reveal", str(run_root), str(bundle)])
    assert repeated.exit_code != 0
    assert "already exists" in repeated.output.lower()
    assert "Traceback" not in repeated.output


def test_generate_reports_invalid_configuration_and_real_filesystem_failures(
    tmp_path: Path,
) -> None:
    """Configuration and destination failures must stay concise and traceback-free."""
    runner = CliRunner()
    invalid = runner.invoke(
        cli.app,
        ["generate", str(tmp_path / "invalid"), "--model", ""],
    )
    assert invalid.exit_code != 0
    assert "invalid request configuration" in invalid.output.lower()
    assert "Traceback" not in invalid.output

    parent_file = tmp_path / "parent-file"
    parent_file.write_text("not a directory")
    uncreatable = runner.invoke(
        cli.app,
        ["generate", str(parent_file / "child"), "--model", "gpt-test", "--seed", "7"],
    )
    assert uncreatable.exit_code != 0
    assert "could not create output directory" in uncreatable.output.lower()
    assert "Traceback" not in uncreatable.output

    unwritable = tmp_path / "unwritable"
    previous_umask = os.umask(0o777)
    try:
        write_failure = runner.invoke(
            cli.app,
            ["generate", str(unwritable), "--model", "gpt-test", "--seed", "7"],
        )
    finally:
        os.umask(previous_umask)
        if unwritable.exists():
            unwritable.chmod(0o700)
    assert write_failure.exit_code != 0
    assert "could not write generated artifacts" in write_failure.output.lower()
    assert "Traceback" not in write_failure.output


def test_run_rejects_default_fixture_drift_and_packet_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the frozen default fixture and hand-authored packet are live-boundary commitments."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    _generate(runner, generated)
    original_digest = cli.FROZEN_CASE_DIGEST
    monkeypatch.setattr(cli, "FROZEN_CASE_DIGEST", "0" * 64)
    frozen_mismatch = runner.invoke(
        cli.app,
        ["run", str(generated), str(tmp_path / "frozen"), "--order-seed", "1"],
        env={"OPENAI_API_KEY": "present"},
    )
    assert frozen_mismatch.exit_code != 0
    assert "frozen case digest" in frozen_mismatch.output.lower()
    monkeypatch.setattr(cli, "FROZEN_CASE_DIGEST", original_digest)

    packet_path = generated / "packet.json"
    packet_payload = json.loads(packet_path.read_text())
    packet_payload["version"] = "tampered"
    packet_path.write_text(json.dumps(packet_payload))
    packet_mismatch = runner.invoke(
        cli.app,
        ["run", str(generated), str(tmp_path / "packet"), "--order-seed", "1"],
        env={"OPENAI_API_KEY": "present"},
    )
    assert packet_mismatch.exit_code != 0
    assert "packet mismatch" in packet_mismatch.output.lower()
    assert "Traceback" not in packet_mismatch.output


def test_run_reports_a_real_manifest_write_failure_before_constructing_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run without its pre-call manifest must abort before the provider seam exists."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    _generate(runner, generated, "--seed", "7")
    constructed = False

    def forbidden_client() -> ScriptedModelClient:
        nonlocal constructed
        constructed = True
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(cli, "OpenAIModelClient", forbidden_client)
    run_root = tmp_path / "unwritable-run"
    previous_umask = os.umask(0o777)
    try:
        result = runner.invoke(
            cli.app,
            ["run", str(generated), str(run_root), "--order-seed", "1"],
            env={"OPENAI_API_KEY": "present"},
        )
    finally:
        os.umask(previous_umask)
        if run_root.exists():
            run_root.chmod(0o700)
    assert result.exit_code != 0
    assert "could not write run manifest" in result.output.lower()
    assert not constructed
    assert "Traceback" not in result.output


def test_judge_and_reveal_report_invalid_roots_without_tracebacks(tmp_path: Path) -> None:
    """Missing private run manifests must stop both downstream commands immediately."""
    runner = CliRunner()
    missing = tmp_path / "missing"
    judge_result = runner.invoke(
        cli.app,
        ["judge", str(missing), str(tmp_path / "blind"), "--blind-seed", "1"],
    )
    reveal_result = runner.invoke(
        cli.app,
        ["reveal", str(missing), str(tmp_path / "blind")],
    )

    for result in (judge_result, reveal_result):
        assert result.exit_code != 0
        assert "invalid run root or run manifest" in result.output.lower()
        assert "Traceback" not in result.output


def test_judge_rejects_a_manifest_without_all_three_terminal_pair_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shaped manifest must not turn a missing three-pair run into an empty blind set."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    for summary in run_root.glob("pair-*/pair_summary.json"):
        summary.unlink()

    result = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )

    assert result.exit_code != 0
    assert "run artifacts do not match run manifest" in result.output.lower()
    assert not bundle.exists()
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "manifest_field",
    [
        "pair_ids",
        "pair_order_seeds",
        "packet_version",
        "packet_off_request_digest",
        "packet_on_request_digest",
        "common_projection_digest",
    ],
)
def test_judge_binds_pair_identity_order_and_request_digests_to_the_run_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_field: str,
) -> None:
    """Changing any committed execution identity must invalidate later blind export."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    manifest_path = run_root / cli.RUN_MANIFEST_FILENAME
    manifest = RunManifest.model_validate_json(manifest_path.read_text())
    updates: dict[str, object] = {
        "pair_ids": ("unexpected-pair", *manifest.pair_ids[1:]),
        "pair_order_seeds": (manifest.pair_order_seeds[0] + 1, *manifest.pair_order_seeds[1:]),
        "packet_version": "unexpected-packet-version",
        "packet_off_request_digest": "0" * 64,
        "packet_on_request_digest": "0" * 64,
        "common_projection_digest": "0" * 64,
    }
    manifest_path.write_text(
        manifest.model_copy(update={manifest_field: updates[manifest_field]}).model_dump_json()
    )

    result = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )

    assert result.exit_code != 0
    assert "run artifacts do not match run manifest" in result.output.lower()
    assert not bundle.exists()
    assert "Traceback" not in result.output


@pytest.mark.parametrize("changed_identity", ["pair_number", "arm_order"])
def test_judge_rejects_terminal_pair_identity_or_seeded_order_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_identity: str,
) -> None:
    """A typed summary still fails when its identity or seeded execution order drifts."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    summary_path = next(run_root.glob("pair-1-*/pair_summary.json"))
    summary = json.loads(summary_path.read_text())
    if changed_identity == "pair_number":
        summary["pair_number"] = 99
        for attempt in summary["attempts"]:
            attempt["start"]["pair_number"] = 99
    else:
        attempt = summary["attempts"][0]
        attempt["start"]["arm_order"].reverse()
        attempt["outcomes"].reverse()
    summary_path.write_text(json.dumps(summary))

    result = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )

    assert result.exit_code != 0
    assert "run artifacts do not match run manifest" in result.output.lower()
    assert not bundle.exists()
    assert "Traceback" not in result.output


def test_judge_reports_existing_export_invalid_source_manifest_and_freeze_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each blind publication failure must preserve the existing gate and explain the stop."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    run_result = _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    )
    assert run_result.exit_code == 0

    pair_path = next(run_root.glob("pair-*/pair_summary.json"))
    original_pair = pair_path.read_text()
    pair_path.write_text("{invalid")
    source_failure = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(tmp_path / "bad-blind"), "--blind-seed", "9"],
    )
    assert source_failure.exit_code != 0
    assert "run artifacts do not match run manifest" in source_failure.output.lower()
    pair_path.write_text(original_pair)

    publication_failure = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(tmp_path / "missing-parent" / "blind"),
            "--blind-seed",
            "9",
        ],
    )
    assert publication_failure.exit_code != 0
    assert "could not export a valid blind bundle" in publication_failure.output.lower()
    assert "Traceback" not in publication_failure.output

    exported = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )
    assert exported.exit_code == 0
    repeated_export = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )
    assert repeated_export.exit_code != 0
    assert "already exists" in repeated_export.output.lower()

    empty_judgments = tmp_path / "empty.json"
    empty_judgments.write_text("[]")
    incomplete = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(empty_judgments),
        ],
    )
    assert incomplete.exit_code != 0
    assert "could not freeze judgments" in incomplete.output.lower()
    assert not (bundle / FROZEN_JUDGMENTS_FILENAME).exists()

    (bundle / MANIFEST_FILENAME).unlink()
    invalid_manifest = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(empty_judgments),
        ],
    )
    assert invalid_manifest.exit_code != 0
    assert "invalid or missing blind bundle manifest" in invalid_manifest.output.lower()
    assert "Traceback" not in invalid_manifest.output


def test_judge_refuses_to_freeze_after_a_committed_public_output_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blind decision changed after export must not receive a valid freeze artifact."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0
    manifest = BlindManifest.model_validate_json((bundle / MANIFEST_FILENAME).read_text())
    output_path = bundle / f"{manifest.ordered_opaque_ids[0]}.json"
    public_output = json.loads(output_path.read_text())
    public_output["decision"]["reasoning"] = "tampered after export"
    output_path.write_text(json.dumps(public_output))
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                judgment.model_dump(mode="json")
                for judgment in _judgments(manifest.ordered_opaque_ids)
            ]
        )
    )

    result = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    )

    assert result.exit_code != 0
    assert "public output digest does not match manifest" in result.output.lower()
    assert not (bundle / FROZEN_JUDGMENTS_FILENAME).exists()
    assert "Traceback" not in result.output


def test_judge_rejects_coordinated_summary_tampering_that_disagrees_with_append_only_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing both summaries must not overwrite the request-level outcome evidence."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0

    summary_path = next(run_root.glob("pair-1-*/pair_summary.json"))
    summary = json.loads(summary_path.read_text())
    attempt_id = summary["attempts"][0]["start"]["attempt_id"]
    outcome_path = summary_path.parent / "attempts" / attempt_id / "outcomes" / "01-packet_off.json"
    original_outcome = json.loads(outcome_path.read_text())
    summary["attempts"][0]["outcomes"][0]["parsed_decision"]["reasoning"] = (
        "coordinated summary tampering"
    )
    summary_path.write_text(json.dumps(summary))
    attempt_summary_path = summary_path.parent / "attempts" / attempt_id / "attempt_summary.json"
    attempt_summary = json.loads(attempt_summary_path.read_text())
    attempt_summary["outcomes"][0]["parsed_decision"]["reasoning"] = (
        "coordinated summary tampering"
    )
    attempt_summary_path.write_text(json.dumps(attempt_summary))
    assert json.loads(outcome_path.read_text()) == original_outcome

    result = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )

    assert result.exit_code != 0
    assert "run artifacts do not match run manifest" in result.output.lower()
    assert not bundle.exists()
    assert "Traceback" not in result.output


def test_judge_rejects_a_forged_attempt_that_orphans_append_only_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewritten pair summary must not hide outcomes under its former attempt directory."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0

    summary_path = next(run_root.glob("pair-1-*/pair_summary.json"))
    summary = json.loads(summary_path.read_text())
    original_attempt_id = summary["attempts"][0]["start"]["attempt_id"]
    original_attempt = summary_path.parent / "attempts" / original_attempt_id
    forged_attempt_id = "forged-attempt"
    forged_attempt = summary_path.parent / "attempts" / forged_attempt_id
    shutil.copytree(original_attempt, forged_attempt)
    start = json.loads((forged_attempt / "attempt_start.json").read_text())
    start["attempt_id"] = forged_attempt_id
    (forged_attempt / "attempt_start.json").write_text(json.dumps(start))
    attempt_summary = json.loads((forged_attempt / "attempt_summary.json").read_text())
    attempt_summary["start"]["attempt_id"] = forged_attempt_id
    (forged_attempt / "attempt_summary.json").write_text(json.dumps(attempt_summary))
    summary["attempts"][0]["start"]["attempt_id"] = forged_attempt_id
    summary_path.write_text(json.dumps(summary))
    assert (original_attempt / "outcomes" / "01-packet_off.json").is_file()

    result = runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    )

    assert result.exit_code != 0
    assert "run artifacts do not match run manifest" in result.output.lower()
    assert not bundle.exists()
    assert "Traceback" not in result.output


@pytest.mark.parametrize("mutation", ("version", "ordered IDs", "exclusions"))
def test_judge_refuses_a_blind_manifest_not_recomputable_from_the_verified_source_before_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """A self-consistent public index changed before freezing must still match source evidence."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0
    manifest = _tamper_blind_manifest(bundle, mutation)
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                judgment.model_dump(mode="json")
                for judgment in _judgments(manifest.ordered_opaque_ids)
            ]
        )
    )

    result = runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    )

    assert result.exit_code != 0
    assert "blind manifest does not match verified source run" in result.output.lower()
    assert not (bundle / FROZEN_JUDGMENTS_FILENAME).exists()
    assert "Traceback" not in result.output


@pytest.mark.parametrize("mutation", ("version", "ordered IDs", "exclusions"))
def test_reveal_refuses_a_coordinated_post_freeze_blind_manifest_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """A rewritten frozen digest cannot authorize labels for a source-divergent manifest."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0
    manifest = BlindManifest.model_validate_json((bundle / MANIFEST_FILENAME).read_text())
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                judgment.model_dump(mode="json")
                for judgment in _judgments(manifest.ordered_opaque_ids)
            ]
        )
    )
    assert runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    ).exit_code == 0
    _cohere_frozen_manifest_digest(bundle, _tamper_blind_manifest(bundle, mutation))

    result = runner.invoke(cli.app, ["reveal", str(run_root), str(bundle)])

    assert result.exit_code != 0
    assert "blind manifest does not match verified source run" in result.output.lower()
    assert not (bundle / REVEAL_DIRECTORY).exists()
    assert "Traceback" not in result.output


def test_reveal_reports_a_corrupt_frozen_gate_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted post-freeze digest must remain a concise terminal error at the CLI."""
    runner = CliRunner()
    generated = tmp_path / "generated"
    run_root = tmp_path / "run"
    bundle = tmp_path / "blind"
    _generate(runner, generated, "--seed", "7")
    assert _run(
        runner,
        monkeypatch,
        generated,
        run_root,
        ScriptedModelClient([ModelReply(parsed_decision=_decision())] * 6),
    ).exit_code == 0
    assert runner.invoke(
        cli.app,
        ["judge", str(run_root), str(bundle), "--blind-seed", "9"],
    ).exit_code == 0
    manifest = BlindManifest.model_validate_json((bundle / MANIFEST_FILENAME).read_text())
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                judgment.model_dump(mode="json")
                for judgment in _judgments(manifest.ordered_opaque_ids)
            ]
        )
    )
    assert runner.invoke(
        cli.app,
        [
            "judge",
            str(run_root),
            str(bundle),
            "--blind-seed",
            "9",
            "--judgments",
            str(judgments_path),
        ],
    ).exit_code == 0
    frozen_path = bundle / FROZEN_JUDGMENTS_FILENAME
    frozen_payload = json.loads(frozen_path.read_text())
    frozen_payload["judgments_digest"] = "0" * 64
    frozen_path.write_text(json.dumps(frozen_payload))

    result = runner.invoke(cli.app, ["reveal", str(run_root), str(bundle)])

    assert result.exit_code != 0
    assert "could not reveal blind results" in result.output.lower()
    assert not (bundle / REVEAL_DIRECTORY).exists()
    assert "Traceback" not in result.output
