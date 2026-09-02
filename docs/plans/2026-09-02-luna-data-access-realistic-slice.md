# Data Access Luna Realistic Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add freeze, suite, and post-reveal uptake tooling so Data Access v2 can run a three-case Luna study on builder-generated packets without a new experiment package.

**Architecture:** Keep `dsx-data-access` as the only runner. A new `realistic.py` module freezes CSV/Parquet cases through `dsx-packet build`, a new `uptake.py` module scores identifier/imbalance/module-citation behavior after reveal, and a new `suite.py` module loops existing `prepare`/`run` over freeze directories and writes a pooled index. Live scoring, SQL, blinding, and the v1 oracle stay unchanged.

**Tech Stack:** Python 3.12, Pydantic v2 immutable contracts, Typer CLI, DuckDB, pytest (100% branch coverage on `dsx.experiments.data_access`), existing OpenAI Responses client.

## Global Constraints

- Reuse Data Access v2 contracts; do not add a third experiment package or `ExperimentSpec` compiler.
- Product `dsx.packet`, `dsx.pipeline`, and `dsx.builders` must not import experiment packages. Experiment code may import builders and packet APIs.
- Packets remain opaque at `prepare`/`run` time. Freeze and uptake may parse builder modules; the live runner must not.
- Do not invent transformation manifests. The freeze command has no `--manifest` option.
- Do not change the v1 oracle, evidence protocol, Terra artifacts, or Context Lift.
- Destinations are exclusive. Failures leave no partial freeze, suite, or uptake directory.
- CLI user errors exit nonzero with `Error: ...` and no traceback.
- Offline tests must not require Hugging Face, `OPENAI_API_KEY`, or live Luna. Operator download and paid runs stay in the protocol doc.
- Coverage gate remains `--cov=dsx.experiments.data_access` with `--cov-branch --cov-fail-under=100`.
- Shared freeze task prompt is exactly: `Recommend a classifier for a 5% manual-review budget. Return the required structured decision. Use factual_claims only for facts you can cite exactly.`
- Eligibility caps: `row_count <= 20000`, `column count <= 40`, target distinct non-null values `<= 10`. Signals: `likely_identifier` finding, `target_class_imbalance` trap, or any `dataset-profile` column with `missing_rate >= 0.05`.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/dsx/experiments/data_access/realistic.py` | Freeze contracts, source inspection, eligibility, `freeze_case`. |
| `src/dsx/experiments/data_access/uptake.py` | Post-reveal uptake records from a decision plus committed packet. |
| `src/dsx/experiments/data_access/suite.py` | Suite config, prepare/run loop, pooled index. |
| `src/dsx/experiments/data_access/cli.py` | Add `freeze`, `suite`, `uptake` commands that call the modules above. |
| `tests/experiments/data_access/test_realistic.py` | Eligibility and freeze tests. |
| `tests/experiments/data_access/test_uptake.py` | Uptake diagnostic tests. |
| `tests/experiments/data_access/test_suite.py` | Suite loop tests with `ScriptedResponsesClient`. |
| `tests/experiments/data_access/test_cli.py` | CLI wiring for the three new commands. |
| `docs/experiments/data-access-luna-realistic.md` | Operator protocol. |
| `docs/experiments/README.md` | Link the follow-on study. |
| `docs/cli-reference.md` | Document `freeze`, `suite`, `uptake`. |
| `docs/specs/2026-09-02-luna-data-access-realistic-slice.md` | Already written; do not rewrite. |

Do not modify `evaluation.py` oracle predicates, `execution.py` fairness, or `docs/experiments/data-access-results.md`.

---

### Task 1: Freeze contracts, inspection, and eligibility

**Files:**
- Create: `src/dsx/experiments/data_access/realistic.py`
- Test: `tests/experiments/data_access/test_realistic.py`

**Interfaces:**
- Consumes: `dsx.builders.models` (`DatasetProfile`, `ColumnsProfile`, `TargetProfile`, `FeatureRisks`, `DataTrap`, module id/type constants), `dsx.packet.tasks.DsTask`, `dsx.packet.assemble_task_packet`, `dsx.experiments.data_access.models.DataAccessContract` / `DatasetFormat`
- Produces:
  - `MAX_ROWS: int = 20_000`
  - `MAX_COLUMNS: int = 40`
  - `MAX_TARGET_CLASSES: int = 10`
  - `MISSING_RATE_THRESHOLD: float = 0.05`
  - `REVIEW_CLASSIFIER_TASK_PROMPT: str`
  - `REVIEW_CLASSIFIER_TASK: DsTask` with `task_id="review-classifier"`, `task_type="review-classifier"`, `objective` equal to the first sentence of the shared prompt, and `module_types=("profile.dataset", "profile.columns", "profile.target", "risk.data_traps", "risk.features")`
  - `EligibilitySignal = Literal["likely_identifier", "target_class_imbalance", "material_missingness"]`
  - `class SourceInspection(DataAccessContract)` with `row_count: int`, `column_names: tuple[str, ...]`, `target_distinct_non_null: int`, `dataset_format: DatasetFormat`
  - `class Eligibility(DataAccessContract)` with `signals: tuple[EligibilitySignal, ...]`, `row_count: int`, `column_count: int`, `target_distinct_non_null: int`, `module_ids: tuple[str, ...]`
  - `def inspect_tabular_source(path: Path, *, target_column: str) -> SourceInspection`
  - `def packet_module_content(packet: DsxPacket, module_id: str, model: type[T]) -> T`
  - `def evaluate_eligibility(packet: DsxPacket, *, target_column: str) -> Eligibility`
  - `def require_review_classifier_task(packet: DsxPacket) -> TaskPacket`

- [ ] **Step 1: Write the failing tests**

Create `tests/experiments/data_access/test_realistic.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dsx.builders.build import build_packet
from dsx.builders.models import PacketBuildRequest
from dsx.packet import DsxPacket
from tests.builders.helpers import write_csv


def _eligible_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(20):
        rows.append(
            {
                "row_id": f"r{index}",
                "label": 1 if index == 0 else 0,
                "nullable": None if index < 2 else index,
                "noise": index,
            }
        )
    return rows


def test_inspect_tabular_source_reads_csv_shape(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import inspect_tabular_source

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    inspection = inspect_tabular_source(path, target_column="label")
    assert inspection.row_count == 20
    assert inspection.column_names == ("row_id", "label", "nullable", "noise")
    assert inspection.target_distinct_non_null == 2
    assert inspection.dataset_format.value == "csv"


def test_inspect_rejects_missing_target(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import inspect_tabular_source

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    with pytest.raises(ValueError, match="target column is not present"):
        inspect_tabular_source(path, target_column="missing")


def test_evaluate_eligibility_accepts_identifier_imbalance_and_missingness(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import evaluate_eligibility

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="eligible-v1"
        )
    ).packet
    eligibility = evaluate_eligibility(packet, target_column="label")
    assert "likely_identifier" in eligibility.signals
    assert "target_class_imbalance" in eligibility.signals
    assert "material_missingness" in eligibility.signals
    assert eligibility.row_count == 20
    assert eligibility.column_count == 4


def test_evaluate_eligibility_rejects_oversized_and_signalless_packets(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import (
        MAX_COLUMNS,
        MAX_ROWS,
        evaluate_eligibility,
    )

    path = write_csv(
        tmp_path / "balanced.csv",
        [
            {"label": 0, "noise": 1},
            {"label": 1, "noise": 2},
        ],
    )
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="tiny-v1"
        )
    ).packet
    with pytest.raises(ValueError, match="no eligibility signal"):
        evaluate_eligibility(packet, target_column="label")

    oversized = packet.model_copy(
        update={
            "modules": tuple(
                module.model_copy(
                    update={
                        "content": {**module.content, "row_count": MAX_ROWS + 1}
                        if module.module_id == "dataset-profile"
                        and isinstance(module.content, dict)
                        else module.content
                    }
                )
                for module in packet.modules
            )
        }
    )
    with pytest.raises(ValueError, match="row_count"):
        evaluate_eligibility(oversized, target_column="label")
    assert MAX_COLUMNS == 40


def test_require_review_classifier_task_passes_on_builder_packet(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import require_review_classifier_task

    path = write_csv(tmp_path / "data.csv", _eligible_rows())
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=path, target_column="label", packet_id="eligible-v1"
        )
    ).packet
    assembled = require_review_classifier_task(packet)
    assert assembled.task.task_id == "review-classifier"
    assert len(assembled.modules) == 5
```

If `packet.model_copy` cannot patch nested content because `content` types disagree, write a helper in the test that rebuilds `DsxPacket` modules by dumping JSON and replacing `row_count` on `dataset-profile`. The rejection assertion is `ValueError` matching `row_count`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/experiments/data_access/test_realistic.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `dsx.experiments.data_access.realistic`.

- [ ] **Step 3: Write minimal implementation**

Create `src/dsx/experiments/data_access/realistic.py` with the constants, `inspect_tabular_source` (DuckDB `read_csv_auto` / `read_parquet` from suffix `.csv` or `.parquet` only; `SELECT count(*)`; `DESCRIBE`; `count(DISTINCT target)` excluding nulls), `packet_module_content`, `evaluate_eligibility`, and `require_review_classifier_task`.

Eligibility algorithm:

1. Parse `dataset-profile`, `column-profile`, `target-profile`, `data-traps`, `feature-risks` with the builder models.
2. `row_count` and `column_count` from `dataset-profile`. Reject if `row_count > 20000` or `column_count > 40`.
3. `target_distinct_non_null = len(target_profile.classes)`. Reject if `> 10`.
4. Collect signals: any `feature-risks` finding (`FeatureRisks.model_validate(content).findings`) with `kind == "likely_identifier"`; any `data-traps` item (module `content` is a JSON **array** of trap objects, not `{"findings": ...}` — validate each element with `DataTrap`) with `kind == "target_class_imbalance"`; any dataset-profile column with `missing_rate >= 0.05`.
5. If `signals` is empty, raise `ValueError("no eligibility signal")`.
6. `require_review_classifier_task` calls `assemble_task_packet(packet, REVIEW_CLASSIFIER_TASK)`.

Inspect must raise `ValueError(f"target column is not present in dataset: {target_column}")` when the target is missing, matching `prepare.py`.

Leave `freeze_case` unimplemented in this task.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/experiments/data_access/test_realistic.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dsx/experiments/data_access/realistic.py tests/experiments/data_access/test_realistic.py
git commit -m "$(cat <<'EOF'
Add Data Access freeze eligibility for builder packets.

EOF
)"
```

---

### Task 2: `freeze_case` and `dsx-data-access freeze`

**Files:**
- Modify: `src/dsx/experiments/data_access/realistic.py`
- Modify: `src/dsx/experiments/data_access/cli.py`
- Modify: `tests/experiments/data_access/test_realistic.py`
- Modify: `tests/experiments/data_access/test_cli.py`

**Interfaces:**
- Consumes: Task 1 functions, `build_packet`, `write_packet_bundle` internals (write `packet.json` / `build-record.json` with `canonical_json()`), `CaseConfig`, `PacketBuildMetrics`, `sha256_file` from `dsx.builders.profiling`
- Produces:
  - `class FreezeNote(DataAccessContract)` with `case_id: str`, `source_id: str`, `license_accepted: Literal[True]`, `inheritance: Literal["datasets_only"]`, `packet_digest: str`, `dataset_file_digest: str`, `module_ids: tuple[str, ...]`, `eligibility_signals: tuple[EligibilitySignal, ...]`, `target_column: str`, `row_count: int`, `column_count: int`
  - `class FreezeResult(DataAccessContract)` with `directory: str`, `case: CaseConfig`, `note: FreezeNote`, `packet_build_metrics: PacketBuildMetrics`
  - `def freeze_case(*, dataset_path: Path, output: Path, case_id: str, target_column: str, source_id: str, packet_id: str | None = None, clock: Callable[[], float] | None = None) -> FreezeResult`
  - CLI: `dsx-data-access freeze DATASET OUTPUT --case-id ID --target COL --source-id SRC --license-accepted [--packet-id ID]`

`freeze_case` behavior:

1. `output` must not exist. Create exclusively; on any exception, `shutil.rmtree(output, ignore_errors=True)` then re-raise (same exclusive idea as packet bundles).
2. `inspect_tabular_source` first. Reject oversize / too many target classes here so huge files never hit `build_packet`.
3. Copy the dataset into `output / dataset_path.name` using `shutil.copy2`.
4. Time `build_packet` with `clock` (default `time.perf_counter`). `PacketBuildMetrics(elapsed_seconds=elapsed, estimated_cost_usd=0.0)`.
5. Write `packet.json` and `build-record.json` using each object's `canonical_json()` plus newline (same as `persist.py`).
6. `require_review_classifier_task` then `evaluate_eligibility`.
7. Write `case.json` as `CaseConfig(case_id=..., task_prompt=REVIEW_CLASSIFIER_TASK_PROMPT, dataset_path=<copied file path as string>, dataset_format=inspection.dataset_format, target_column=..., oracle_version="v1")`.
8. Write `packet-build-metrics.json` and `freeze-note.json` with indent-2 JSON via exclusive create (`open(..., "x")`).
9. Default `packet_id` is `case_id`.

- [ ] **Step 1: Write the failing tests**

Append to `test_realistic.py`:

```python
def test_freeze_case_writes_builder_bundle_and_metrics(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import (
        REVIEW_CLASSIFIER_TASK_PROMPT,
        freeze_case,
    )

    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    ticks = iter((1.0, 1.25))
    result = freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze",
        case_id="case-a",
        target_column="label",
        source_id="datascibench:example-a",
        clock=lambda: next(ticks),
    )
    freeze = tmp_path / "freeze"
    assert (freeze / "packet.json").is_file()
    assert (freeze / "build-record.json").is_file()
    assert (freeze / "case.json").is_file()
    assert (freeze / "packet-build-metrics.json").is_file()
    assert (freeze / "freeze-note.json").is_file()
    assert (freeze / "data.csv").is_file()
    assert result.packet_build_metrics.elapsed_seconds == 0.25
    assert result.packet_build_metrics.estimated_cost_usd == 0.0
    assert result.case.task_prompt == REVIEW_CLASSIFIER_TASK_PROMPT
    assert result.note.inheritance == "datasets_only"
    assert result.note.license_accepted is True
    assert "likely_identifier" in result.note.eligibility_signals


def test_freeze_case_refuses_existing_output_and_missing_signals(tmp_path) -> None:
    from dsx.experiments.data_access.realistic import freeze_case

    existing = tmp_path / "freeze"
    existing.mkdir()
    source = write_csv(tmp_path / "data.csv", _eligible_rows())
    with pytest.raises(FileExistsError):
        freeze_case(
            dataset_path=source,
            output=existing,
            case_id="case-a",
            target_column="label",
            source_id="src",
        )
    balanced = write_csv(
        tmp_path / "balanced.csv",
        [{"label": 0, "noise": 1}, {"label": 1, "noise": 2}],
    )
    dest = tmp_path / "rejected"
    with pytest.raises(ValueError, match="no eligibility signal"):
        freeze_case(
            dataset_path=balanced,
            output=dest,
            case_id="case-b",
            target_column="label",
            source_id="src",
        )
    assert not dest.exists()
```

Add to `test_cli.py`:

```python
def test_freeze_cli_writes_exclusive_directory(tmp_path: Path) -> None:
    from tests.builders.helpers import write_csv

    rows = [
        {
            "row_id": f"r{index}",
            "label": 1 if index == 0 else 0,
            "nullable": None if index < 2 else index,
            "noise": index,
        }
        for index in range(20)
    ]
    dataset = write_csv(tmp_path / "data.csv", rows)
    output = tmp_path / "freeze"
    result = runner.invoke(
        app,
        [
            "freeze",
            str(dataset),
            str(output),
            "--case-id",
            "case-a",
            "--target",
            "label",
            "--source-id",
            "datascibench:example-a",
            "--license-accepted",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Frozen Data Access case" in result.output
    assert (output / "packet.json").is_file()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/experiments/data_access/test_realistic.py::test_freeze_case_writes_builder_bundle_and_metrics tests/experiments/data_access/test_cli.py::test_freeze_cli_writes_exclusive_directory -v`
Expected: FAIL with `freeze_case` not defined / no `freeze` command.

- [ ] **Step 3: Write minimal implementation**

Implement `freeze_case` in `realistic.py`. Add Typer command `freeze` in `cli.py` that requires `--license-accepted` (flag, `is_flag=True` / `typer.Option(..., help=...)`). On `FileExistsError` / `ValueError` / `OSError` / `ValidationError`, `_abort`. Print:

```text
Frozen Data Access case: {output}
Packet digest: {digest}
```

Do not add `--manifest`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/experiments/data_access/test_realistic.py tests/experiments/data_access/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dsx/experiments/data_access/realistic.py src/dsx/experiments/data_access/cli.py tests/experiments/data_access/test_realistic.py tests/experiments/data_access/test_cli.py
git commit -m "$(cat <<'EOF'
Add Data Access freeze command for generated packets.

EOF
)"
```

---

### Task 3: Post-reveal uptake diagnostic

**Files:**
- Create: `src/dsx/experiments/data_access/uptake.py`
- Test: `tests/experiments/data_access/test_uptake.py`

**Interfaces:**
- Consumes: `DataAccessDecision`, `EvidenceKind`, `Arm`, `DsxPacket`, builder `FeatureRisks` / `DatasetProfile` / trap findings, `packet_module_content` from `realistic.py`
- Produces:
  - `class IdentifierUptake(DataAccessContract)` with `columns: tuple[str, ...]`, `excluded: tuple[str, ...]`, `missed: tuple[str, ...]`, `all_excluded: bool`
  - `class ImbalanceUptake(DataAccessContract)` with `applicable: bool`, `acknowledged: bool | None`
  - `class PacketModuleCitations(DataAccessContract)` with `column_profile: bool`, `feature_risks: bool`, `data_traps: bool`
  - `class UptakeRecord(DataAccessContract)` with `arm: Arm`, `repetition_id: str`, `identifiers: IdentifierUptake`, `imbalance: ImbalanceUptake`, `packet_module_citations: PacketModuleCitations | None`
  - `def identifier_columns_for_arm(packet: DsxPacket, *, arm: Arm, target_column: str) -> tuple[str, ...]`
  - `def module_id_for_pointer(packet: DsxPacket, pointer: str) -> str | None`
  - `def evaluate_uptake(decision: DataAccessDecision, packet: DsxPacket, *, arm: Arm, repetition_id: str, target_column: str) -> UptakeRecord`
  - `def evaluate_run_uptake(run_root: Path) -> tuple[UptakeRecord, ...]`

Rules (deterministic):

- Packet-bearing arms (`dsx_packet`, `packet_and_full_data`): identifier columns are `feature-risks` findings of kind `likely_identifier`.
- `full_data`: identifier columns are non-target `column-profile` columns with `uniqueness_rate` equal to `1.0` under `math.isclose(..., rel_tol=1e-12, abs_tol=1e-12)` (v1 oracle `likely_id`, not the fully-populated builder rule).
- `all_excluded` is true iff every identifier column appears in `decision.excluded_columns`.
- Imbalance is applicable when any `data-traps` content element (JSON array of `DataTrap` objects) has `kind == "target_class_imbalance"`. If applicable, `acknowledged` is true iff `"imbalance"` appears in the lowercase concatenation of `reasoning`, `recommendation`, and `limitations`. If not applicable, `acknowledged` is `None`.
- `packet_module_citations` is `None` for `full_data`. Otherwise, scan `factual_claims[*].evidence` of kind `packet_json_pointer`, map each pointer through `module_id_for_pointer`, and set the three booleans if `column-profile`, `feature-risks`, or `data-traps` were cited.
- `module_id_for_pointer`: pointer must start with `/modules/<int>` where `<int>` indexes `packet.modules`. Invalid pointers return `None`.
- `evaluate_run_uptake` loads the prepared manifest packet as `DsxPacket` (fail if it is not a builder packet), walks completed `ArmRun` values in the run root the same way `blind.py` discovers eligible outputs, parses `decision_json` as `DataAccessDecision`, and skips non-completed arms.

Look at `export_blind` in `src/dsx/experiments/data_access/blind.py` for how run roots store repetition JSON. Reuse that discovery rather than inventing a second ledger layout. If the helper is not importable without cycles, duplicate the small “load repetition files / completed arms” loop in `uptake.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/experiments/data_access/test_uptake.py` with at least:

```python
from dsx.builders.build import build_packet
from dsx.builders.models import PacketBuildRequest
from dsx.experiments.data_access.evaluation import DataAccessDecision, Metric
from dsx.experiments.data_access.models import Arm
from tests.builders.helpers import write_csv


def _decision(**overrides: object) -> DataAccessDecision:
    payload = {
        "primary_metric": Metric.recall_at_5_percent,
        "supporting_metrics": (),
        "review_budget_fraction": 0.05,
        "split_strategy": "stratified",
        "excluded_columns": ("row_id",),
        "reasoning": "Exclude the identifier because of class imbalance.",
        "limitations": ("synthetic",),
        "recommendation": "rank for review",
        "factual_claims": (
            {
                "claim_id": "c1",
                "statement": "row_id is unique",
                "predicate": "likely_id",
                "arguments": {"column": "row_id"},
                "asserted_value": True,
                "evidence": ({"kind": "packet_json_pointer", "pointer": "/modules/4/content"},),
            },
        ),
        "narrative_claim_ids": ("c1",),
    }
    payload.update(overrides)
    return DataAccessDecision.model_validate(payload)


def test_packet_arm_uses_feature_risk_findings(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake

    rows = [
        {
            "row_id": f"r{index}",
            "label": 1 if index == 0 else 0,
            "nullable": None if index < 2 else index,
            "noise": index,
        }
        for index in range(20)
    ]
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=write_csv(tmp_path / "data.csv", rows),
            target_column="label",
            packet_id="eligible-v1",
        )
    ).packet
    record = evaluate_uptake(
        _decision(),
        packet,
        arm=Arm.dsx_packet,
        repetition_id="repetition-001",
        target_column="label",
    )
    assert record.identifiers.columns == ("row_id",)
    assert record.identifiers.all_excluded is True
    assert record.imbalance.applicable is True
    assert record.imbalance.acknowledged is True
    assert record.packet_module_citations is not None
    assert record.packet_module_citations.feature_risks is True


def test_full_data_arm_has_no_packet_citations_and_misses_exclusions(tmp_path) -> None:
    from dsx.experiments.data_access.uptake import evaluate_uptake

    rows = [
        {
            "row_id": f"r{index}",
            "label": 1 if index == 0 else 0,
            "nullable": None if index < 2 else index,
            "noise": index,
        }
        for index in range(20)
    ]
    packet = build_packet(
        PacketBuildRequest(
            dataset_path=write_csv(tmp_path / "data.csv", rows),
            target_column="label",
            packet_id="eligible-v1",
        )
    ).packet
    record = evaluate_uptake(
        _decision(excluded_columns=(), reasoning="no issues", factual_claims=()),
        packet,
        arm=Arm.full_data,
        repetition_id="repetition-001",
        target_column="label",
    )
    assert record.identifiers.missed == ("row_id",)
    assert record.imbalance.acknowledged is False
    assert record.packet_module_citations is None
```

Confirm `feature-risks` is module index 4 in a no-manifest builder packet (`dataset-profile`, `column-profile`, `target-profile`, `data-traps`, `feature-risks`). If a test fails because the index differs, read `packet.modules` in the test and point at the actual `feature-risks` index instead of hard-coding `4`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/experiments/data_access/test_uptake.py -v`
Expected: FAIL with import error for `uptake`.

- [ ] **Step 3: Write minimal implementation**

Implement `uptake.py` as specified. Do not call this from `judge` or `reveal`. Do not put uptake fields on public blind outputs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/experiments/data_access/test_uptake.py tests/experiments/data_access/test_realistic.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dsx/experiments/data_access/uptake.py tests/experiments/data_access/test_uptake.py
git commit -m "$(cat <<'EOF'
Add post-reveal uptake diagnostics for generated packets.

EOF
)"
```

---

### Task 4: `evaluate_run_uptake` and `dsx-data-access uptake`

**Files:**
- Modify: `src/dsx/experiments/data_access/uptake.py`
- Modify: `src/dsx/experiments/data_access/cli.py`
- Modify: `tests/experiments/data_access/test_uptake.py`
- Modify: `tests/experiments/data_access/test_cli.py`

**Interfaces:**
- Consumes: `evaluate_uptake`, run-root discovery from Task 3, `DataAccessManifest.packet.value` parsed as `DsxPacket`
- Produces:
  - `class UptakeReport(DataAccessContract)` with `records: tuple[UptakeRecord, ...]`
  - `def write_uptake_report(report: UptakeReport, output: Path) -> None` exclusive directory containing `uptake.json`
  - CLI: `dsx-data-access uptake RUN_ROOT OUTPUT`

- [ ] **Step 1: Write the failing tests**

Add a test that reuses the existing scripted three-arm run pattern from `tests/experiments/data_access/test_execution.py` (`_manifest`, `ScriptedResponsesClient`, `_decision` / `_reply`, `run_experiment` with `repetitions=1` if the helper manifest allows). After a completed run whose packet is a real builder packet (freeze or `build_packet` JSON written into prepare), call `evaluate_run_uptake(run_root)` and assert one record per completed arm.

If constructing a full `run_experiment` in this test is too coupled, freeze a tiny eligible CSV, `prepare_manifest` with that packet, `run_experiment` with `ExperimentLimits(repetitions=1)` and three scripted completed decisions, then `evaluate_run_uptake`.

CLI test: `runner.invoke(app, ["uptake", str(run_root), str(output)])` exit 0, `output/uptake.json` exists. A second invoke to the same output exits 1 with `Error:` and `already exists`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/experiments/data_access/test_uptake.py tests/experiments/data_access/test_cli.py -k uptake -v`
Expected: FAIL on missing `evaluate_run_uptake` / `uptake` command.

- [ ] **Step 3: Write minimal implementation**

Implement report writing and the CLI command. If the committed packet is not a `DsxPacket`, `_abort("committed packet is not a DSX Packet")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/experiments/data_access/test_uptake.py tests/experiments/data_access/test_cli.py tests/experiments/data_access/test_realistic.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dsx/experiments/data_access/uptake.py src/dsx/experiments/data_access/cli.py tests/experiments/data_access/test_uptake.py tests/experiments/data_access/test_cli.py
git commit -m "$(cat <<'EOF'
Add Data Access uptake command for completed run roots.

EOF
)"
```

---

### Task 5: Suite loop

**Files:**
- Create: `src/dsx/experiments/data_access/suite.py`
- Modify: `src/dsx/experiments/data_access/cli.py`
- Test: `tests/experiments/data_access/test_suite.py`
- Modify: `tests/experiments/data_access/test_cli.py`

**Interfaces:**
- Consumes: `freeze_case` output layout, `prepare_manifest`, `run_experiment`, `ExperimentLimits`, `PricingSnapshot`, `OpaquePacket`, `ModelConfig`, `ResponsesClient`
- Produces:
  - `class SuiteCase(DataAccessContract)` with `case_id: str`, `freeze_directory: str`, `order_seed: int`
  - `class SuiteConfig(DataAccessContract)` with `study_id: Literal["data-access-luna-realistic"]`, `model_identifier: str`, `pricing_path: str`, `cases: tuple[SuiteCase, ...]` (`min_length=1`)
  - `class SuiteCaseResult(DataAccessContract)` with `case_id: str`, `status: Literal["complete", "failed"]`, `freeze_directory: str`, `input_directory: str | None = None`, `run_root: str | None = None`, `error: str | None = None`
  - `class SuiteIndex(DataAccessContract)` with `study_id: Literal["data-access-luna-realistic"]`, `cases: tuple[SuiteCaseResult, ...]`
  - `def run_suite(config: SuiteConfig, output: Path, *, client: ResponsesClient, limits: ExperimentLimits | None = None, system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> SuiteIndex`

`run_suite` behavior:

1. Refuse if `output` exists.
2. For each case, read `freeze_directory/case.json`, `packet.json`, `packet-build-metrics.json`.
3. `prepare_manifest` into `output / case_id / inputs` with `database_path=.../dataset.duckdb`. Use `limits or ExperimentLimits()`.
4. Write `inputs/manifest.json` the same way `cli.prepare` does.
5. Create `output / case_id / run`, write `run_manifest.json` as `DataAccessRunManifest`, call `run_experiment`.
6. If a case raises, record `status="failed"` and `error=str(error)`, do not delete other case directories, continue.
7. Write `output / suite-index.json` exclusively at the end (or after each case using a temp file rename if you need crash safety; tests only require the final file).
8. CLI `dsx-data-access suite SUITE_CONFIG OUTPUT` requires `OPENAI_API_KEY` like `run`, constructs `OpenAIResponsesClient()`, uses default three-repetition limits. Tests call `run_suite(..., client=ScriptedResponsesClient(...), limits=ExperimentLimits(repetitions=1, model_calls_per_arm=4, sql_attempts_per_arm=3, arm_wall_clock_seconds=30))`.

- [ ] **Step 1: Write the failing tests**

`test_suite.py`:

```python
def test_run_suite_prepares_and_runs_one_frozen_case(tmp_path) -> None:
    from dsx.experiments.data_access.execution import ScriptedResponsesClient
    from dsx.experiments.data_access.models import ExperimentLimits, PricingSnapshot, TokenPrice
    from dsx.experiments.data_access.realistic import freeze_case
    from dsx.experiments.data_access.suite import SuiteCase, SuiteConfig, run_suite
    from tests.builders.helpers import write_csv
    from tests.experiments.data_access.test_execution import _decision, _reply

    source = write_csv(
        tmp_path / "data.csv",
        [
            {
                "row_id": f"r{index}",
                "label": 1 if index == 0 else 0,
                "nullable": None if index < 2 else index,
                "noise": index,
            }
            for index in range(20)
        ],
    )
    freeze = freeze_case(
        dataset_path=source,
        output=tmp_path / "freeze",
        case_id="case-a",
        target_column="label",
        source_id="src",
    )
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1.0),
            output=TokenPrice(usd_per_million_tokens=2.0),
            source="test",
            effective_date="2026-09-02",
        ).model_dump_json(),
        encoding="utf-8",
    )
    config = SuiteConfig(
        study_id="data-access-luna-realistic",
        model_identifier="gpt-5.6-luna",
        pricing_path=str(pricing),
        cases=(
            SuiteCase(case_id="case-a", freeze_directory=str(tmp_path / "freeze"), order_seed=7),
        ),
    )
    index = run_suite(
        config,
        tmp_path / "suite",
        client=ScriptedResponsesClient([_reply(decision=_decision())] * 3),
        limits=ExperimentLimits(repetitions=1, model_calls_per_arm=4, sql_attempts_per_arm=3),
    )
    assert index.cases[0].status == "complete"
    assert (tmp_path / "suite" / "suite-index.json").is_file()
    assert freeze.case.case_id == "case-a"
```

Import paths for `_decision` / `_reply` / `TokenPrice` must match the real test/execution modules. If `_decision` is not exported, copy the small JSON helper from `test_execution.py` into `test_suite.py` rather than importing a private test function. Import `TokenPrice` from `dsx.experiments.data_access.models`.

Second test: two cases, first freeze is valid, second `freeze_directory` is missing; first `complete`, second `failed`, first run root still exists.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/experiments/data_access/test_suite.py -v`
Expected: FAIL with import error for `suite`.

- [ ] **Step 3: Write minimal implementation**

Implement `suite.py` and CLI `suite`. Failed cases must not raise out of `run_suite`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/experiments/data_access/test_suite.py tests/experiments/data_access/test_cli.py tests/experiments/data_access/test_realistic.py tests/experiments/data_access/test_uptake.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dsx/experiments/data_access/suite.py src/dsx/experiments/data_access/cli.py tests/experiments/data_access/test_suite.py tests/experiments/data_access/test_cli.py
git commit -m "$(cat <<'EOF'
Add Data Access suite loop over frozen cases.

EOF
)"
```

---

### Task 6: Protocol docs and quality gate

**Files:**
- Create: `docs/experiments/data-access-luna-realistic.md`
- Modify: `docs/experiments/README.md`
- Modify: `docs/cli-reference.md`
- Modify: `TODOS.md` (mark the representative-datasets TODO as in progress / point at this study)
- Modify: `docs/experiments/data-access.md` (one short paragraph at the end pointing at the realistic-slice protocol; do not change v2 semantics)

**Interfaces:**
- Consumes: CLI flags and artifact layouts from Tasks 2–5
- Produces: operator-facing protocol that matches the spec

- [ ] **Step 1: Write the protocol**

`docs/experiments/data-access-luna-realistic.md` must include:

- Study identity: new Data Access study; do not pool Terra results.
- Hugging Face `zd21/DataSciBench` is gated; freeze only after download.
- Eligibility rules and shared 5% review-classifier prompt (copy the exact prompt string).
- Commands, in order:

```text
uv run dsx-data-access freeze DATA.csv artifacts/data-access-luna-realistic/CASE/freeze \
  --case-id CASE --target TARGET --source-id datascibench:ID --license-accepted

uv run dsx-data-access suite suite.json artifacts/data-access-luna-realistic/suite

uv run dsx-data-access judge RUN_ROOT BLIND --blind-seed SEED
uv run dsx-data-access judge RUN_ROOT BLIND --blind-seed SEED --judgments judgments.json
uv run dsx-data-access reveal RUN_ROOT BLIND
uv run dsx-data-access uptake RUN_ROOT artifacts/data-access-luna-realistic/CASE/uptake
```

- `judge` / `reveal` remain per-case. One blind bundle per case.
- Stop if fewer than three tables pass eligibility.
- Stop before live calls if the Luna id is not callable with `OPENAI_API_KEY` + `MODEL_ID`.
- Allowed claim paragraph copied from the spec.
- Operator inputs: source ids, Luna model id, pricing snapshot, order seeds, judge.

Suite JSON example:

```json
{
  "study_id": "data-access-luna-realistic",
  "model_identifier": "gpt-5.6-luna",
  "pricing_path": "pricing.json",
  "cases": [
    {
      "case_id": "case-a",
      "freeze_directory": "artifacts/data-access-luna-realistic/case-a/freeze",
      "order_seed": 731
    }
  ]
}
```

Update `docs/experiments/README.md` with a third row: **Data Access (Luna realistic slice)** linking the new protocol, described as a follow-on study not a third harness.

Add `freeze`, `suite`, and `uptake` to the Data Access section of `docs/cli-reference.md`.

- [ ] **Step 2: Run the offline quality gate**

Run:

```bash
uv run pytest -m "not live" \
  --cov=dsx.packet \
  --cov=dsx.pipeline \
  --cov=dsx.builders \
  --cov=dsx.experiments.context_lift \
  --cov=dsx.experiments.data_access \
  --cov-branch --cov-fail-under=100
uv run ruff check .
uv run mypy src
```

Expected: all pass. If coverage misses a CLI abort branch, add a CLI test that hits it rather than `# pragma: no cover` except for `if __name__ == "__main__"`.

- [ ] **Step 3: Commit**

```bash
git add docs/experiments/data-access-luna-realistic.md docs/experiments/README.md docs/cli-reference.md docs/experiments/data-access.md TODOS.md tests src
git commit -m "$(cat <<'EOF'
Document the Luna realistic Data Access slice.

EOF
)"
```

---

## Operator work after this plan (not implementation tasks)

These stay out of the code tasks because they need gated data and a paid Luna id:

1. Accept Hugging Face terms and download candidate tabular files.
2. Freeze until three cases pass; stop rather than filling with the synthetic pilot.
3. Commit pricing snapshot and Luna `MODEL_ID`.
4. Run the suite, three per-case blind reviews, reveal, then uptake.
5. Write `docs/experiments/data-access-luna-realistic-results.md` from the revealed reports.

---

## Self-review

**Spec coverage**

| Spec section | Task |
| --- | --- |
| New study identity, reuse Data Access v2 | 5–6 (no new package) |
| Builder packets, default modules, no fake history | 1–2 (`freeze` has no manifest flag) |
| Task assembly as freeze gate | 1 `require_review_classifier_task` |
| Packet-build metrics timed, not read from build-record | 2 |
| Eligibility caps and signals | 1–2 |
| Shared 5% review prompt | 2 |
| Freeze tree under artifacts | 2 + protocol |
| Stop if fewer than three tables | 6 protocol |
| Suite prepare/run, per-case judge | 5–6 |
| Luna must use existing Responses client | 5 CLI + 6 |
| Uptake after reveal, not in blind bundle | 3–4 |
| Oracle unchanged | no `evaluation.py` edits |
| Docs / claim boundary | 6 |

**Placeholders:** none. Suite tests copy a decision helper rather than “similar to Task N” without code.

**Type consistency:** `FreezeNote`, `Eligibility`, `SuiteConfig`, `UptakeRecord`, and CLI names are reused across later tasks as defined in earlier tasks.
