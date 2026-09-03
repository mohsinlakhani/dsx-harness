---
type: contributor quickstart
title: DSX Harness Quickstart
description: A task-routing guide for DSX Packet product work, immutable bundle construction, the separate Context Lift and Data Access experiments, evidence controls, and focused verification.
tags: [quickstart, dsx, packets, experiments, testing]
verified:
  - by: openwiki/0.5.0
    at: 2026-09-02T23:04:01.230Z
sources:
  - id: openwiki-source-115b2dad781e2a2c5b5a980d
    resource: repo://docs/architecture.md
  - id: openwiki-source-f9e58738f34925cff6d8405c
    resource: repo://docs/cli-reference.md
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-7c04aff66a3114e5f646a5fe
    resource: repo://src/dsx/builders/cli.py
  - id: openwiki-source-048f23554e3c388bc4e478a9
    resource: repo://src/dsx/builders/persist.py
  - id: openwiki-source-a60f6d8a7603949c13ca5701
    resource: repo://src/dsx/experiments/data_access/cli.py
  - id: openwiki-source-d27619f2d0141d7beab2da0b
    resource: repo://src/dsx/experiments/data_access/sql_tool.py
  - id: openwiki-source-e93a98cf122c957572580368
    resource: repo://src/dsx/packet/tasks.py
  - id: openwiki-source-a78944f4bf8a933cd3dfac1c
    resource: repo://tests/builders/test_isolation.py
  - id: openwiki-source-25e7c45a05d4f54447083d03
    resource: repo://tests/builders/test_persist.py
  - id: openwiki-source-9105ec4a942138677ad54c86
    resource: repo://tests/experiments/data_access/test_execution.py
  - id: openwiki-source-e1ae3cf05e6f752a9765836f
    resource: repo://tests/experiments/data_access/test_sql_tool.py
  - id: openwiki-source-7f3c928bbd4c10aa3d25f9a6
    resource: repo://tests/packet/test_tasks.py
generated: { by: "openwiki/0.5.0", at: "2026-09-02T23:04:01.230Z" }
---

# DSX Harness Quickstart

DSX harness develops inspectable, evidence-backed **DSX Packets** for data-science decisions while keeping reusable product code separate from completed experiment protocols. Start by identifying the boundary you are changing; do not treat the two experiments as interchangeable evaluations or as a place to add general packet functionality.

## Choose the right route

| If you need to… | Start here | Then use |
| --- | --- | --- |
| Change packet identity, modules, canonical commitments, task selection, or declared transformation lineage | [System Boundaries](/openwiki/architecture/system-boundaries.md) and [DSX Packets and History](/openwiki/concepts/dsx-packets-and-history.md) | `src/dsx/packet/` or `src/dsx/pipeline/`; keep these product packages independent of experiment runners. |
| Produce context from a CSV or Parquet dataset | [Building Immutable Packet Bundles](/openwiki/workflows/building-packet-bundles.md) | `dsx-packet build`; use a new output directory and optionally supply a transformation manifest or predecessor bundle. |
| Reproduce or audit the fixed packet-on/off pilot | [Context Lift Controlled Comparison](/openwiki/workflows/context-lift-experiment.md) | `dsx-context-lift` (or historical alias `dsx-pilot`); preserve its frozen schemas and terminology. |
| Compare opaque packet context, direct data discovery, and both | [Data Access Three-Arm Experiment](/openwiki/workflows/data-access-experiment.md) | `dsx-data-access`; treat the prepared input, private run root, and public blind bundle as distinct evidence boundaries. |
| Change persistence, retries, blind review, reveal, credentials, or cost handling | [Evidence Integrity and Blind Review](/openwiki/operations/evidence-integrity-and-blind-review.md) | Preserve commitments and append-only artifacts; do not overwrite or resume an evidence destination. |
| Select tests for a change | [Verification Strategy](/openwiki/testing/verification-strategy.md) | Run the smallest owner suite first, then broaden only when the changed boundary requires it. |

## Product path: construct a packet bundle

`DsxPacket` is the reusable, versioned envelope for a dataset digest and independently versioned JSON modules. A `DsTask` declares required module types, and task assembly returns a source- and dataset-bound projection or fails if required context is missing. This makes task routing an extension point without introducing a second packet storage format.

The normal operational entrypoint is `dsx-packet build`:

```bash
uv run dsx-packet build data/train.parquet artifacts/packet-bundle \
  --target label \
  --packet-id fraud-v1 \
  --manifest data/transforms.json
```

The input must be CSV or Parquet and the target must exist. The command profiles the current dataset, can validate declared transformation history, and emits a packet plus a build record; the raw dataset is not copied into the bundle. A previous validated bundle can establish packet revision lineage with `--previous-bundle`.

Treat `OUTPUT` as a publication destination, not a workspace: it must be new. The writer reserves it, stages canonical JSON in a sibling temporary directory, reparses and checks the packet/build-record commitments (and manifest when present), then renames the validated stage into place. An existing destination is refused, and failed publication must not be replaced or resumed.

For module order, history handling, risk findings, and all builder failure conditions, follow [Building Immutable Packet Bundles](/openwiki/workflows/building-packet-bundles.md) rather than duplicating its protocol here.

## Pick an experiment deliberately

### Context Lift: frozen two-arm pilot

Context Lift asks whether *supplied* context changes a decision in one fixed synthetic case. `generate` writes the seeded case, hand-authored packet, request configuration, and the two rendered requests; it proves that `context.profile_packet` is the only difference between `packet_off` and `packet_on`. `run` revalidates those inputs, writes its manifest before live calls, and executes three sequential pairs. `judge` exports/finalizes blind review, and `reveal` publishes assignments only after judgment freeze.

Use a local generation first:

```bash
uv sync --all-groups
uv run dsx-context-lift generate artifacts/context-lift/generated --model offline-example
```

Only its `run` command needs `OPENAI_API_KEY` and can incur provider cost. Do not use Context Lift to answer the direct-data-access question or rename its historical artifacts: that would compromise reproducibility.

### Data Access: independent three-arm protocol

Data Access compares `dsx_packet`, `full_data`, and `packet_and_full_data` for a committed case. Ordinary `prepare` accepts packet JSON as opaque content, materializes the configured source into DuckDB, and commits those inputs. `run` revalidates them before provider use; the packet-only arm has no tool, the full-data arm receives only the bounded `query_data` tool, and the combined arm receives both contexts. The remaining `judge`, `reveal`, and `uptake` stages operate on that experiment's own artifacts.

A minimal prepare/run sequence is:

```bash
uv run dsx-data-access prepare case.json dsx-packet.json artifacts/data-access/inputs \
  --model "$MODEL_ID" --pricing pricing.json
uv run dsx-data-access run artifacts/data-access/inputs artifacts/data-access/run \
  --order-seed 731
```

`run` needs `OPENAI_API_KEY` and may incur cost. SQL discovery is intentionally narrow: it permits one read-only `SELECT`, `WITH`, or `DESCRIBE dataset` query, with bounded results; rejected or failed queries remain evidence rather than being silently discarded. Use `freeze` and `suite` only for the realistic-study workflow documented on the Data Access page.

## Evidence rules that apply to live work

Both protocols make artifacts auditable by committing inputs and persisting request material before a provider call. Outcomes and tool events are append-only, and infrastructure exhaustion triggers a fresh whole pair or repetition rather than mixing old and new arm results. Keep run roots private while reviewers receive only opaque public outputs. A complete, digest-checked judgment freeze is required before reveal maps opaque outputs back to official arms.

Operationally, use a fresh destination after any completed or interrupted invocation, preserve complete failure output, and keep `OPENAI_API_KEY` out of command arguments and artifacts. A public blind seed supports reproducibility but is not secrecy for someone who also has the labeled private run.

## Validate the change you made

Start narrow and quiet, retaining the complete output if it fails. Examples:

```bash
# Packet contracts or task projection
uv run pytest tests/packet/test_models.py tests/packet/test_tasks.py -q

# Transformation graph
uv run pytest tests/pipeline/test_graph.py -q

# Packet build CLI and product/experiment isolation
uv run pytest tests/builders/test_cli.py tests/builders/test_isolation.py -q

# A changed experiment execution boundary
uv run pytest tests/experiments/context_lift/test_runner.py -q
uv run pytest tests/experiments/data_access/test_execution.py -q
```

For blind-boundary or SQL-policy work, use the corresponding focused suites in [Verification Strategy](/openwiki/testing/verification-strategy.md). Before a broad or release-facing change, run the offline gate:

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
uv build
```

The `live` test layer is opt-in and paid; reserve it for a real provider integration change after the scripted/offline boundary tests pass. Source code and tests are authoritative: investigate a brief's unknowns as verification gaps rather than adding behavior merely because it was suggested.
