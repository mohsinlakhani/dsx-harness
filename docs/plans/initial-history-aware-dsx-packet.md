# Initial history-aware DSX Packet builder

## Objective

Implement one small, end-to-end product increment that proves three capabilities:

1. DSX can retain declared data-transformation history.
2. A `DsxPacket` can carry independent modules for different context capabilities.
3. A validated graph derived from the transformation history can inform packet contents and
   surface data traps before an agent writes code.

The increment builds a `DsxPacket/v1` once, before an agent run. It accepts a CSV or Parquet
dataset, an explicit target column, and an optional DSX-native transformation manifest. It
writes an immutable packet bundle that existing experiments can consume.

Do not add task inference, continuous recompilation, agent context-retrieval tools, a graph
database, pipeline adapters, or arbitrary Python analysis in this increment.

## Existing boundaries to preserve

- Keep `DsxPacket`, `PacketModule`, `DsTask`, and `TaskPacket` backward compatible.
- Keep `PacketModule.content` arbitrary JSON; typed payloads are validators/builders layered on
  top of the stable envelope.
- Product packet, builder, and pipeline code must not import from either experiment package.
- Do not change the frozen Context Lift schemas or execution protocol.
- Do not change Data Access execution semantics. It should continue receiving the generated
  packet as opaque JSON.
- Preserve strict immutable Pydantic contracts, canonical JSON commitments, exclusive output
  destinations, strict mypy, Ruff, and the repository's 100% branch-coverage gate.

## User-facing interface

### CLI

Register a new command:

```text
dsx-packet build DATASET OUTPUT \
  --target TARGET_COLUMN \
  --packet-id PACKET_ID \
  [--manifest MANIFEST_JSON] \
  [--previous-bundle PREVIOUS_OUTPUT]
```

Rules:

- `DATASET` must be an existing `.csv` or `.parquet` file.
- `--target` is required and must name a column in the current dataset.
- `--packet-id` is required and must satisfy the existing `PacketId` contract.
- `OUTPUT` must not exist. Never overwrite or merge into an existing directory.
- Build into a temporary sibling directory and atomically rename it to `OUTPUT` only after all
  artifacts have been serialized and validated.
- Remove the temporary directory after any failure.
- On success, print the output path, packet digest, dataset digest, revision, and module IDs.

The immutable output bundle contains:

```text
OUTPUT/
├── packet.json
├── build-record.json
└── manifest.json          # only when --manifest was supplied
```

Do not copy any dataset into the bundle.

### Python API

Expose the following from a new `dsx.builders` package:

```python
def build_packet(request: PacketBuildRequest) -> PacketBuildResult: ...

def write_packet_bundle(result: PacketBuildResult, output: Path) -> None: ...
```

`PacketBuildRequest` contains:

- current dataset path;
- explicit target column;
- packet ID;
- optional parsed transformation manifest;
- optional directory against which manifest-relative snapshot paths are resolved;
- optional validated previous build record and previous packet digest.

`PacketBuildResult` contains:

- the completed `DsxPacket`;
- its `PacketBuildRecord`;
- the normalized manifest when one was supplied.

Keep computation separate from persistence: `build_packet` may read datasets but does not create
the output bundle. `write_packet_bundle` performs the exclusive, atomic write.

## Transformation manifest and graph

### Persisted contracts

Add a `dsx.pipeline` package containing immutable contracts for:

- `TransformationManifest`
- `DatasetSnapshot`
- `TransformationStep`
- `SnapshotRole`
- `TransformationOperation`

Manifest v1 has this conceptual shape:

```json
{
  "schema_version": "dsx-transform-manifest/v1",
  "pipeline_id": "fraud-training",
  "current_snapshot_id": "train-balanced",
  "snapshots": [
    {
      "snapshot_id": "raw",
      "digest": "<64 lowercase hex characters>",
      "role": "source",
      "path": "data/raw.parquet",
      "format": "parquet"
    },
    {
      "snapshot_id": "train-natural",
      "digest": "<64 lowercase hex characters>",
      "role": "train",
      "path": "data/train-natural.parquet",
      "format": "parquet"
    },
    {
      "snapshot_id": "train-balanced",
      "digest": "<64 lowercase hex characters>",
      "role": "train",
      "path": "data/train-balanced.parquet",
      "format": "parquet"
    }
  ],
  "steps": [
    {
      "step_id": "split",
      "operation": "split",
      "inputs": ["raw"],
      "outputs": ["train-natural"],
      "parameters": {}
    },
    {
      "step_id": "oversample",
      "operation": "augment",
      "inputs": ["train-natural"],
      "outputs": ["train-balanced"],
      "parameters": {"method": "random-oversampling"}
    }
  ]
}
```

The initial operation vocabulary is:

```text
filter, join, derive, rename, drop, split,
sample, augment, custom
```

Snapshot roles are:

```text
source, intermediate, train, validation, test
```

Contract rules:

- Every snapshot and step has a stable, unique ID.
- Every snapshot declares a SHA-256 digest.
- `path` is optional. When present, `format` is required and must be `csv` or `parquet`.
- Step inputs and outputs are non-empty tuples of snapshot IDs.
- `parameters` is standard JSON and defaults to an empty object.
- Relative paths resolve against the directory containing the input manifest.
- Persist the normalized manifest with its declared relative paths unchanged; do not expose
  environment-specific absolute paths in `manifest.json`.

### Graph derivation and validation

Derive a bipartite graph in memory:

```text
DatasetSnapshot -> TransformationStep -> DatasetSnapshot
```

Use standard Python mappings and traversal; do not add NetworkX or another graph dependency.
Provide only the internal operations required by this increment:

- stable topological ordering;
- snapshot ancestors;
- steps and snapshots on lineage leading to the current snapshot;
- checks that one step precedes another on at least one path;
- lookup of immediate inputs and outputs for an augmentation step.

Fail manifest validation when:

- IDs are duplicated;
- a step references a missing snapshot;
- a snapshot has more than one producer;
- a non-source snapshot has no producer;
- a source snapshot has a producer;
- the graph contains a cycle;
- `current_snapshot_id` is missing or not reachable from a source.

Dataset evidence rules:

- Compute snapshot identity as SHA-256 of source file bytes in v1.
- The positional `DATASET` argument is authoritative for reading the current snapshot, but its
  digest must equal the manifest's current snapshot digest.
- If a historical snapshot has a declared path and the file exists, verify its digest before
  reading it.
- A historical path that is omitted or does not exist is unavailable history, not a build
  failure.
- A historical file that exists but disagrees with its declared digest is a hard failure.

## Packet modules and builders

Retain `DsxPacket.schema_version == "dsx-packet/v1"`. Add immutable Pydantic payload models and
builders for the following module types.

### `profile.dataset/v1`

Use module ID `dataset-profile`. Include:

- current snapshot ID;
- row count;
- ordered column summaries containing name, DuckDB type, missing count, and missing rate.

### `profile.target/v1`

Use module ID `target-profile`. Include:

- target column;
- current snapshot ID;
- null and non-null counts;
- per-class values, counts, and rates;
- majority-class rate;
- accessible before/after target distributions for augmentation steps relevant to the current
  snapshot.

Exclude null target values from class-rate denominators while reporting them separately. Sort
class entries deterministically by their canonical JSON representation so packet bytes do not
depend on database row order.

### `history.transforms/v1`

Use module ID `transformation-history`. Include:

- pipeline ID and manifest digest;
- current snapshot ID;
- relevant steps in stable topological order, including operation, inputs, outputs, and
  parameters;
- accessible historical snapshot IDs;
- unavailable historical snapshot IDs.

Omit this module when no manifest was supplied.

### `risk.data_traps/v1`

Use module ID `data-traps`. Its content is a tuple of typed `DataTrap` records and may be empty.
A `DataTrap` includes:

- deterministic finding ID;
- kind;
- warning severity;
- concise message;
- related snapshot IDs and step IDs;
- structured details needed to inspect the finding;
- evidence references.

Module order is always:

```text
dataset-profile
target-profile
transformation-history   # when present
data-traps
```

Evidence references must bind computed facts to the relevant dataset snapshot digest and bind
history claims to the canonical manifest digest. Do not introduce an evidence resolver in this
increment.

## Trap rules

All traps are warnings. They never prevent packet generation or agent execution.

### Target class imbalance

Emit `target_class_imbalance` when:

- only one non-null class is observed; or
- for two or more non-null classes, `minimum_class_count / maximum_class_count < 0.20`.

Do not emit at exactly `0.20`. When no non-null target values exist, emit the same trap with
structured details showing zero observed classes rather than failing the entire build.

### Augmentation before split

Emit `augmentation_before_split` when an `augment` step is upstream of a `split` step on a path
that reaches the current snapshot. A train-only augmentation downstream of a split does not
trigger it.

### Augmentation on evaluation data

Emit `augmentation_on_evaluation` when an augmentation step directly produces, or is an ancestor
of, a snapshot whose role is `validation` or `test`.

### Augmentation changed target distribution

For an augmentation step with exactly one input and one output whose files are both accessible
and valid:

- profile the target on both snapshots;
- compare rates over the union of their non-null class values, treating an absent class as rate
  zero;
- calculate the maximum absolute class-rate change;
- emit `augmentation_changed_target_distribution` when that change is at least `0.05`.

Do not speculate when either side is unavailable, the target is absent, or the step has multiple
inputs or outputs. Preserve the unavailable snapshot IDs in the history module instead.

## Build records and revision history

Add an immutable `PacketBuildRecord` with:

```text
schema_version = "dsx-packet-build/v1"
build_id
built_at
revision
packet_id
packet_digest
previous_packet_digest
dataset_path
dataset_digest
target_column
manifest_digest
module descriptors (ID, type, schema version)
```

`build_id` is a newly generated opaque ID. `built_at` is an aware UTC timestamp serialized in a
single canonical format.

When `--previous-bundle` is absent:

- set `revision = 1`;
- set `previous_packet_digest = null`.

When it is present:

- parse its `packet.json` as `DsxPacket`;
- parse its `build-record.json` as `PacketBuildRecord`;
- recompute the previous packet digest and require it to match the record;
- set the new revision to the previous revision plus one;
- set `previous_packet_digest` to the validated prior digest.

Do not require the new and old packet IDs or dataset digests to match; the explicit predecessor
link defines the history. Do not implement packet diffs or a mutable history index.

## Error and cleanup behavior

Reject the build with a clear message when:

- the current dataset is missing, unreadable, empty, or unsupported;
- CSV/Parquet parsing fails;
- the target column is missing;
- the manifest or graph is invalid;
- the current dataset contradicts the current manifest snapshot;
- an existing historical file contradicts its declared digest;
- a previous bundle is incomplete or internally inconsistent;
- an output destination already exists.

Validate every generated Pydantic artifact and recompute the final packet digest before committing
the temporary bundle.

## Implementation sequence

1. Add manifest contracts and in-memory graph validation/traversal.
2. Add tabular profiling shared by the dataset and target module builders.
3. Add typed module payloads and deterministic packet assembly.
4. Add the four trap detectors.
5. Add build-record generation and previous-bundle validation.
6. Add atomic bundle persistence and the `dsx-packet build` CLI.
7. Add the Data Access compatibility test and documentation updates.

The coding agent should keep each layer independently testable and should not generalize an
abstraction unless the behavior above needs it.

## Test plan

### Manifest and graph contracts

- Round-trip a valid linear, branching, joining, and multi-output manifest through JSON.
- Reject duplicate snapshot IDs and duplicate step IDs.
- Reject missing input/output references, multiple producers, invalid source producers, orphaned
  non-source snapshots, a missing current snapshot, an unreachable current snapshot, and cycles.
- Resolve a relative historical path from the manifest directory.
- Produce a stable topological order independent of input collection ordering.
- Correctly answer ancestor and step-order queries for branches and joins.

### Dataset and target profiling

- Produce equivalent semantic profiles for matching CSV and Parquet fixtures.
- Reject unsupported formats, unreadable data, an empty dataset, and a missing target.
- Report target nulls separately and exclude them from rate denominators.
- Handle string, numeric, boolean, and null class values as standard JSON.
- Produce deterministic column and class ordering, packet JSON, and packet digest.
- Build without a manifest and omit only the transformation-history module.

### Historical evidence

- Accept the CLI dataset when it matches the current manifest snapshot digest.
- Reject a current or existing historical file with a mismatched digest.
- Mark an omitted or missing historical path unavailable and continue.
- Profile accessible snapshots before and after an augmentation step.
- Include only steps relevant to the current snapshot lineage.

### Trap detection

- Emit imbalance for zero or one observed non-null class.
- Emit imbalance below a `0.20` class-count ratio and not at or above it.
- Emit augmentation-before-split for `source -> augment -> split -> current`.
- Do not emit it for `source -> split -> augment(train) -> current`.
- Emit augmentation-on-evaluation for augmented validation/test lineage.
- Emit distribution-change at exactly `0.05` and above, but not below it.
- Compare the union of class values when a class appears on only one side.
- Skip distribution comparison for unavailable snapshots or multi-input/output augmentation.
- Require each finding to carry the expected snapshot, step, and evidence references.

### Build history and bundle persistence

- Create exactly the expected files for builds with and without a manifest.
- Refuse an existing output without changing it.
- Leave no partial output directory after validation or write failures.
- Validate and link a previous bundle, incrementing its revision.
- Reject a previous bundle whose packet bytes, packet digest, or build record disagree.
- Confirm the bundle does not contain copied dataset files.
- Reparse all written artifacts into their public contracts.

### CLI and experiment compatibility

- Run `dsx-packet build` end to end for CSV and Parquet fixtures.
- Verify clear CLI failures for each invalid input category.
- Load generated `packet.json` through Data Access `OpaquePacket` and `prepare_manifest` without
  changing its contents.
- Run the entire existing offline test suite unchanged.
- Run `ruff check .`, strict `mypy src`, `uv build`, and the updated 100% branch-coverage command.

## Acceptance criteria

The increment is complete when a fixture representing:

```text
raw imbalanced data -> split -> oversample training data
```

produces a deterministic packet that:

- reports the current dataset and target profile;
- contains the declared transformation history;
- exposes accessible pre- and post-augmentation distributions;
- warns that augmentation changed the target distribution;
- does not incorrectly warn that downstream train-only augmentation happened before the split;
- records the exact packet digest, data digest, manifest digest, revision, and predecessor;
- is accepted unchanged by the existing Data Access preparation path;
- passes all existing and new offline quality gates.

## Explicitly deferred

- probabilistic task inference and deterministic task routing in the CLI;
- agent-facing `list_context` or `get_context` tools;
- continuous packet recompilation;
- recorded agent observations;
- assumptions/questions modules;
- identifier, leakage, drift, and data-quality detectors beyond the four traps above;
- graph persistence, graph databases, visual editors, or mutation servers;
- pandas, sklearn, dbt, Airflow, or arbitrary Python adapters;
- automatic parsing of transformation code;
- blocking findings or policy enforcement;
- detailed packet diffs and mutable history indexes.
