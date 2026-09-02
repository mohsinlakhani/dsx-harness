# Typed column profile and likely-identifier risks

## Objective

Implement the next bounded DSX Packet feature after the initial history-aware builder:

1. compute reusable column-cardinality facts for the current dataset;
2. identify exact, fully populated non-target identifier candidates;
3. publish facts and findings as two independently selectable packet modules; and
4. preserve deterministic packets, immutable contracts, evidence binding, previous-bundle
   compatibility, and the repository's offline quality gates.

This increment extends packet generation only. Do not add an agent experiment, task router,
generic `ExperimentSpec`, automatic feature exclusion, near-unique thresholds, name heuristics,
correlation checks, semantic leakage inference, or blocking policy.

## Implementation base and boundaries

Implement this work on top of `feat/history-aware-dsx-packet` at or after commit `f16e328`. That
branch contains `dsx.builders`, `dsx.pipeline`, the `dsx-packet build` CLI, and their tests. If the
branch has already been merged, use the merged equivalent as the base.

Preserve these existing boundaries:

- Keep `DsxPacket.schema_version == "dsx-packet/v1"` and leave the packet envelope unchanged.
- Keep all existing module payloads and schema versions backward compatible.
- Keep `PacketModule.content` arbitrary JSON; validate the two new payloads with builder-owned
  immutable Pydantic models.
- Do not add fields or flags to `PacketBuildRequest`, `build_packet`, or `dsx-packet build`.
- Product packet, builder, and pipeline code must not import either experiment package.
- Data Access must continue consuming generated packets as opaque JSON.
- Findings are advisory. They never modify a feature set, synthesize an exclusion list, prevent
  persistence, or block agent execution.
- Preserve canonical JSON commitments, exclusive atomic bundle writes, strict mypy, Ruff, and
  100% branch coverage.

## Packet contracts

Add the following constants to `dsx.builders.models`:

```python
COLUMN_PROFILE_MODULE_ID: ModuleName = "column-profile"
FEATURE_RISKS_MODULE_ID: ModuleName = "feature-risks"
COLUMN_PROFILE_TYPE: ModuleName = "profile.columns"
FEATURE_RISKS_TYPE: ModuleName = "risk.features"
COLUMN_PROFILE_SCHEMA = "profile.columns/v1"
FEATURE_RISKS_SCHEMA = "risk.features/v1"
```

### `profile.columns/v1`

Define an immutable `ColumnCardinality` model with:

```text
name                non-empty string
duckdb_type         non-empty string
non_null_count      integer >= 0
distinct_count      integer >= 0
uniqueness_rate     float in [0.0, 1.0]
```

Define an immutable `ColumnsProfile` model with:

```text
current_snapshot_id non-empty string
row_count           integer >= 1
columns             non-empty tuple[ColumnCardinality, ...]
```

Contract rules:

- Include every current-dataset column, including the target, in source schema order.
- `distinct_count` means distinct non-null values and must use DuckDB `count(DISTINCT column)`
  semantics.
- `uniqueness_rate` is `distinct_count / row_count`, not `distinct_count / non_null_count`.
- Require unique column names.
- For each column require `distinct_count <= non_null_count <= row_count`.
- Recompute the expected rate and reject a supplied rate that is not equal under
  `math.isclose(..., rel_tol=1e-12, abs_tol=1e-12)`.
- Reject non-finite rates through the existing standard-JSON/Pydantic boundaries.

The module content is the JSON dump of `ColumnsProfile`, not a bare list.

### `risk.features/v1`

Define a `FeatureRiskKind` literal containing only `"likely_identifier"` and a
`FeatureRiskAction` literal containing only `"exclude_or_verify"`.

Define an immutable `FeatureRiskFinding` model with:

```text
finding_id          non-empty string
kind                "likely_identifier"
severity            "warning"
column              non-empty string
message             non-empty string
row_count           integer >= 1
non_null_count      integer >= 0
distinct_count      integer >= 0
uniqueness_rate     float in [0.0, 1.0]
recommended_action  "exclude_or_verify"
evidence_refs       non-empty tuple[str, ...]
```

Because v1 represents only exact identifier candidates, validate each finding as follows:

- `non_null_count == row_count`;
- `distinct_count == row_count`;
- `uniqueness_rate` is `1.0` under the same `math.isclose` tolerance; and
- `evidence_refs` contains no duplicates.

Define an immutable `FeatureRisks` model with:

```text
current_snapshot_id non-empty string
target_column       non-empty string
findings            tuple[FeatureRiskFinding, ...]  # may be empty
```

Require unique finding IDs and unique finding columns, and reject any finding whose column is
the declared target.

The module content is the JSON dump of `FeatureRisks`, including `"findings": []` when no
candidate exists. Do not add the new kind to `DataTrap` or change `risk.data_traps/v1`; feature
risks are independently selectable context.

## Profiling implementation

Refactor `dsx.builders.profiling` so dataset summaries and column-cardinality facts are derived
from the same opened DuckDB table:

1. Add a `profile_table_and_columns(...) -> tuple[DatasetProfile, ColumnsProfile]` entry point.
2. It must detect or accept the same explicit CSV/Parquet format as `profile_table`, open the
   table once, compute the row count once, reject an empty dataset exactly as today, and read
   `DESCRIBE dataset` once.
3. For each described column, use quoted identifiers and compute both:
   - `count(column)` for the non-null count; and
   - `count(DISTINCT column)` for the distinct non-null count.
4. Derive the existing `ColumnSummary.missing_count` as `row_count - non_null_count` and its
   missing rate as `missing_count / row_count`.
5. Build the new `ColumnCardinality` from the same query results.
6. Preserve schema order rather than sorting columns alphabetically.
7. Keep the existing `profile_table(...) -> DatasetProfile` callable and behavior. Implement it
   as a compatibility wrapper over the shared profiling path so current callers and tests do
   not break.

`build_packet` must call the combined entry point. Do not load or parse the dataset a second
time merely to produce `profile.columns`; the separate existing target profiling pass may
remain unchanged in this increment.

If DuckDB cannot parse the dataset or calculate these aggregates, normalize the exception to
the existing clear `ValueError` dataset-profiling failure boundary and do not write a partial
bundle.

## Identifier detector

Add `src/dsx/builders/feature_risks.py` with a pure deterministic detector:

```python
def detect_likely_identifiers(
    profile: ColumnsProfile,
    *,
    target_column: str,
    evidence_refs: Sequence[str],
) -> tuple[FeatureRiskFinding, ...]: ...
```

Walk `profile.columns` in source order. Emit a finding if and only if all of these conditions
hold:

```text
column.name != target_column
column.non_null_count == profile.row_count
column.distinct_count == profile.row_count
```

Do not use the DuckDB type or column name as a detection signal. Do not impose a minimum row
count. In particular, a fully unique non-target column in a small valid dataset still matches
the v1 rule.

For a matching column use:

```text
finding_id = "likely-identifier:<column-name>"
kind = "likely_identifier"
severity = "warning"
recommended_action = "exclude_or_verify"
message = 'Column <JSON-quoted name> has one distinct non-null value per row and may be an identifier.'
```

Use `json.dumps(column.name, ensure_ascii=False)` for the JSON-quoted name in the message so
spaces, quotes, Unicode, and control characters are represented deterministically. Copy the
profile's counts and rate into the finding and preserve the supplied evidence-reference order
after removing duplicates.

## Packet assembly

In `dsx.builders.build`:

1. Obtain `dataset_profile` and `columns_profile` from the shared profiling pass.
2. After computing the current dataset evidence tuple, call `detect_likely_identifiers` with
   `dataset_evidence == (f"sha256:{dataset_digest}",)`.
3. Pass the two new payloads into `_assemble_modules`.
4. Always emit both modules; there is no opt-in setting.
5. Set the module-level evidence references for both modules to the current dataset evidence.
6. Give every feature-risk finding the same current dataset evidence reference.

The exact module order is:

```text
dataset-profile
column-profile
target-profile
transformation-history   # only when a manifest is present
data-traps
feature-risks
```

The existing build-record code should derive descriptors from this final sequence. Do not add
special-case build-record fields. Repeated builds from identical inputs must retain identical
packet JSON and packet digests; build IDs and timestamps remain intentionally different.

An older valid previous bundle that lacks the new modules remains a valid predecessor. Its
packet digest is linked normally, the revision increments, and the newly built packet includes
both new modules.

Do not export the new payload or detector models from `dsx.builders.__init__`; follow the
existing convention in which the top-level builder package exposes the build API and records,
while module payload contracts live in `dsx.builders.models`.

## CLI and persistence behavior

Do not add CLI arguments. A normal command such as:

```bash
dsx-packet build data.csv output \
  --target label \
  --packet-id case-v1
```

automatically writes the two new modules into `packet.json`. The existing success output's
module-ID list must include `column-profile` and `feature-risks` in packet order.

Atomic persistence and cleanup behavior are unchanged. `packet.json` and `build-record.json`
must reparse through their contracts before the temporary bundle is renamed. The dataset is
still not copied into the output bundle.

## Tests to add and update

### Profiling tests

Extend `tests/builders/test_profiling.py` to cover:

- semantically equivalent column counts and uniqueness rates for matching CSV and Parquet;
- source-order preservation;
- exact counts for unique, duplicated, constant, partially null, and all-null columns;
- `count(DISTINCT ...)` excluding nulls;
- column names requiring SQL identifier quoting;
- the compatibility `profile_table` wrapper returning the unchanged `DatasetProfile` shape;
- unsupported, malformed, unreadable, and empty inputs preserving existing errors; and
- model validation branches for duplicate names, impossible count relationships, and a rate
  inconsistent with its counts.

Use a fixture with at least these columns:

```text
row_id            fully populated and unique
duplicate_value   fully populated with duplicates
nullable_unique   distinct among present values but contains a null
constant_value    one repeated value
all_null          entirely null
label             fully populated and unique, but declared as the target
```

### Detector tests

Add `tests/builders/test_feature_risks.py` and cover:

- `row_id` is emitted as an exact likely identifier;
- the duplicated, nullable, constant, and all-null columns are not emitted;
- a unique target is never emitted;
- two matching non-target columns produce two findings in source order;
- matching does not depend on column type or identifier-like names;
- a candidate with spaces, quotes, or Unicode has the exact deterministic finding ID and
  JSON-quoted message;
- a profile with no candidates returns an empty tuple;
- evidence references are copied and deduplicated deterministically; and
- all `FeatureRiskFinding` and `FeatureRisks` validation branches: inconsistent exact counts,
  non-unit rate, duplicate evidence, duplicate IDs, duplicate columns, and a target-column
  finding.

### Packet build and persistence tests

Update `tests/builders/test_build.py` and related persistence/CLI assertions to cover:

- builds without a manifest use module IDs in this exact order:
  `dataset-profile`, `column-profile`, `target-profile`, `data-traps`, `feature-risks`;
- builds with a manifest insert `transformation-history` between `target-profile` and
  `data-traps`;
- `profile.columns` contains all expected cardinality facts;
- `risk.features` contains a finding for an exact ID and an empty findings list when none
  exists;
- both module-level and finding-level evidence references equal the current dataset digest
  reference;
- the build record contains descriptors for both new schema versions in packet order;
- two identical builds have identical canonical packet JSON and packet digests;
- a legacy predecessor packet/build record without the new modules is accepted, increments
  the revision, and yields a new packet containing the modules;
- all written artifacts reparse and no dataset file is copied; and
- simulated profiling or persistence failures leave neither the destination nor a temporary
  sibling directory.

Replace index-based module access in existing tests where inserting `column-profile` would
change an index; locate modules by module ID unless the test is explicitly asserting order.

Update CLI tests to assert that successful output lists both new module IDs and that invalid
input still returns a concise nonzero `Error:` response without a traceback or partial output.

### Experiment compatibility and isolation

- Update the existing Data Access compatibility test to pass the expanded generated packet
  through `OpaquePacket` and `prepare_manifest`, asserting byte-for-byte/canonical-content
  preservation.
- Keep experiment schemas, execution semantics, and evaluator code unchanged.
- Extend the builder isolation test so the new module and detector cannot import
  `dsx.experiments.*`.

## Documentation updates

- Update the README packet-builder section to list the two new default modules.
- Update `docs/cli-reference.md` to describe the column-profile fields, the exact identifier
  rule, empty risk modules, and warning-only behavior.
- Update `docs/roadmap.md` to mark exact likely-ID detection as implemented while retaining
  near-unique and semantic leakage checks as future work.
- Keep the architecture's next product increments focused on task routing, module ablation,
  and post-decision checks; this increment does not implement them.

## Quality gates

Run all of the following from the implementation worktree:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -m "not live" \
  --cov=dsx.packet \
  --cov=dsx.pipeline \
  --cov=dsx.builders \
  --cov=dsx.experiments.context_lift \
  --cov=dsx.experiments.data_access \
  --cov-branch --cov-fail-under=100
uv build
```

Do not weaken exclusions, add `pragma: no cover` for reachable new behavior, or lower the
coverage threshold to make the gate pass.

## Acceptance criteria

The increment is complete when a CSV or Parquet dataset containing a unique `row_id`, a unique
target, duplicated features, and nullable values produces a deterministic packet that:

- includes complete source-ordered column cardinality facts;
- warns only about the unique, fully populated non-target `row_id`;
- reports the exact counts, unit uniqueness rate, advisory action, and dataset evidence;
- always contains both new modules, even when the findings tuple is empty;
- preserves history-aware modules and previous-bundle revision links;
- is accepted unchanged by the Data Access preparation path; and
- passes every offline quality gate above.

## Explicitly deferred

- near-unique thresholds or configurable detection policy;
- identifier inference from column names or data types;
- alternate target encodings, suspicious predictors, or post-outcome leakage;
- train/test entity overlap, temporal leakage, drift, and correlated missingness;
- automatic exclusions, blocking findings, or policy enforcement;
- task inference, packet routing, packet ablation experiments, and generic experiment specs;
- interactive correction, packet diffs, and agent-facing context tools.
