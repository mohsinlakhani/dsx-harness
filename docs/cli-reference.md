# CLI reference

## DSX Packet

`dsx-packet` builds an immutable history-aware packet bundle from a CSV or Parquet dataset.
The generated `packet.json` remains opaque to Data Access: `prepare` consumes it as JSON
without interpreting module contents.

```text
dsx-packet build DATASET OUTPUT \
  --target TARGET_COLUMN \
  --packet-id PACKET_ID \
  [--manifest MANIFEST_JSON] \
  [--previous-bundle PREVIOUS_OUTPUT]
```

### Arguments

| Name | Type | Required | Meaning |
| --- | --- | --- | --- |
| `DATASET` | path | yes | Existing `.csv` or `.parquet` file used as the current snapshot. The file is not copied into the bundle. |
| `OUTPUT` | path | yes | New exclusive directory for the packet bundle. The path must not exist. |

### Options

| Option | Type | Required | Meaning |
| --- | --- | --- | --- |
| `--target` | string | yes | Target column that must exist in the current dataset. |
| `--packet-id` | string | yes | Packet identity matching the `PacketId` contract. |
| `--manifest` | path | no | Optional DSX transformation-manifest JSON. Relative snapshot paths resolve from the manifest directory. |
| `--previous-bundle` | path | no | Optional previous packet bundle used to increment `revision` and record the predecessor digest. |

On success the command prints the output path, packet digest, dataset digest, revision, and
module IDs. The bundle contains `packet.json`, `build-record.json`, and `manifest.json` when a
manifest was supplied. Builds write into a temporary sibling directory and rename it only after
every artifact validates.

The packet always includes these modules, in order: `dataset-profile`, `column-profile`,
`target-profile`, `data-traps`, and `feature-risks`. When a manifest is supplied,
`transformation-history` is inserted between `target-profile` and `data-traps`.

`column-profile` (`profile.columns/v1`) records every current-dataset column, including the
target, in source schema order. Each column has `name`, `duckdb_type`, `non_null_count`,
`distinct_count` (distinct non-null values), and `uniqueness_rate` (`distinct_count / row_count`).

`feature-risks` (`risk.features/v1`) reports exact likely-identifier candidates: a non-target
column that is fully populated (`non_null_count == row_count`) and unique (`distinct_count ==
row_count`). The target is never emitted. When no candidate exists the module is still written
with `"findings": []`. Findings are warning-only and advisory: they never exclude features or
block persistence.

Expected user errors exit nonzero with an `Error:` message and no internal traceback. Destinations
are exclusive: a second build to the same path is refused, and a failed build leaves no partial
output directory.

Example:

```bash
uv run dsx-packet build data/train.parquet artifacts/packet-bundle \
  --target label \
  --packet-id fraud-v1 \
  --manifest data/transforms.json
```

## Data Access

`dsx-data-access` implements the separate **Data Access v2** comparison: `dsx_packet` receives
canonical opaque packet JSON, `full_data` receives an audited read-only SQL tool over the same
prepared dataset, and `packet_and_full_data` receives both. Its inputs, run, blind bundle, and
reveal artifacts are independent of the frozen Context Lift (`dsx-context-lift`) artifacts below.

```text
dsx-data-access freeze DATASET OUTPUT --case-id ID --target COL --source-id SRC --license-accepted [--packet-id ID]
dsx-data-access suite SUITE_CONFIG OUTPUT
dsx-data-access prepare CASE_CONFIG DSX_PACKET OUTPUT --model MODEL --pricing PRICING_JSON
dsx-data-access run INPUT_DIRECTORY RUN_ROOT --order-seed SEED
dsx-data-access judge RUN_ROOT BLIND_BUNDLE --blind-seed SEED [--judgments FILE]
dsx-data-access reveal RUN_ROOT BLIND_BUNDLE
dsx-data-access uptake RUN_ROOT OUTPUT
```

`freeze` copies a local CSV or Parquet file, times `dsx-packet build`, and writes an exclusive
freeze directory (`case.json`, `packet.json`, `build-record.json`, `packet-build-metrics.json`,
`freeze-note.json`). `--license-accepted` is required. There is no `--manifest` flag: do not
invent transformation history. Optional `--packet-id` defaults to `--case-id`. Freeze fails
nonzero if the table exceeds 20,000 rows or 40 columns, the target has more than 10 distinct
non-null values, task assembly fails, or no eligibility signal fires.

`suite` takes a JSON config (`study_id` must be `data-access-luna-realistic`) and an exclusive
output directory. It requires `OPENAI_API_KEY`, sequences existing `prepare` and `run` over
each freeze directory, and writes `suite-index.json`. A failed case is marked ineligible; other
case artifacts are kept. `judge` and `reveal` remain unchanged per-case commands: one blind
bundle per run root.

`uptake` writes an exclusive post-reveal diagnostic (`uptake.json`) for completed arms in a
private run root. It is not part of the public blind bundle.

`prepare` is local and exclusively writes a materialized DuckDB database and an immutable
manifest. It accepts CSV, Parquet, and pilot-case JSON as configured in `CASE_CONFIG`. Packet
JSON remains opaque to the framework. The manifest commits source and materialized dataset
digests, packet, task/configuration, pricing, oracle version, limits, and query tool schema.

`run` and `suite` require `OPENAI_API_KEY`. `run` revalidates every prepared digest
before a provider call, writes an exclusive private run root, randomizes all three arms inside
each repetition from `--order-seed`, and records raw model/tool events append-only. The data
tool is restricted to one `SELECT`, `WITH`, or `DESCRIBE dataset` query per call; results are
capped at 1,000 rows and 64 KiB by default. Rejected, invalid, timed-out, oversized, and failed
queries remain observable failed discovery attempts.

`judge` without judgments writes a new public blind bundle. It publishes decisions and factual
claim statements only. It excludes treatment identity, packet contents, data, SQL, tool
results, evidence locators, usage, cost, automatic scoring, and repetition identifiers. With a
complete judgments JSON array, it revalidates the source and public commitments and freezes
the judgments exclusively. `reveal` requires that freeze, then exclusively joins qualitative
judgments with official arms and the private objective/operational report.

See [Data Access](experiments/data-access.md) for the v2 protocol and claim boundary, and
[Data Access Luna realistic slice](experiments/data-access-luna-realistic.md) for freeze, suite,
and uptake. Destinations are exclusive; malformed or tampered inputs fail nonzero without
overwriting evidence.

## Context Lift

`dsx-context-lift` runs the fixed Context Lift experiment. The older `dsx-pilot` command is a
compatibility alias. Both expose four
top-level commands: `generate`, `run`, `judge`, and `reveal`.

All persisted artifacts are UTF-8, indented JSON validated by immutable Pydantic contracts.
Destination directories and freeze/reveal files are exclusive: commands never resume or
overwrite them. Expected user errors exit nonzero with an `Error:` message and no internal
traceback.

## `generate`

Generate the synthetic case and hand-authored packet, render both request arms, prove that
`context.profile_packet` is the only request delta, and write a new input bundle.

```text
dsx-context-lift generate [OPTIONS] OUTPUT_DIRECTORY
```

### Argument

| Name | Type | Required | Meaning |
| --- | --- | --- | --- |
| `OUTPUT_DIRECTORY` | path | yes | New directory for generated artifacts. The path must not exist. |

### Options

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--model` | string | no | Exact model identifier recorded in both rendered requests. Defaults to `MODEL_ID` from `.env`. |
| `--seed` | integer | `20260819` | Pilot-case generation seed. The default seed is checked against the frozen case digest. |
| `--system-prompt` | string | `Return only a valid structured analysis matching the requested response schema.` | System prompt shared by both arms. |
| `--response-schema-name` | string | `analysis_decision` | Structured-output schema name recorded in the request and sent to the provider; 1–64 ASCII letters, digits, underscores, or hyphens. |
| `--help` | flag | n/a | Show command help and exit. |

Example:

```bash
uv run dsx-context-lift generate pilot-generated --model offline-example
```

For normal live runs, create `.env` from `.env.example` and set `MODEL_ID` once; then omit
`--model`. Environment variables supplied by the shell take precedence over `.env` values.

The command prints the output path, the case digest, both full-request digests, and the
common-projection digest. It does not print the 5,000 case rows.

### Artifacts

| Path below `OUTPUT_DIRECTORY` | Contract | Content |
| --- | --- | --- |
| `case.json` | `PilotCase` | Generation seed, task text, 5,000 rows, and fixed 5% manual-review fraction. |
| `packet.json` | `Packet` | Hand-authored dataset facts, metric/split guidance, exclusions, limitations, and evidence references. |
| `request_configuration.json` | `RequestConfiguration` | Exact model identifier, shared system prompt, and response schema name. |
| `rendered_requests.json` | `RenderedRequests` | Packet-off/on requests, individual request digests, and their shared common-projection digest. |

For seed `20260819`, the case digest must equal
`57eec293b8b511ac9c1cf244eded3dddd483f375b47435bd92db67dcc5af5c9a`.

### Error conditions

The command exits nonzero if the request configuration is invalid, including an unsafe or
overlong response schema name, or if the default case no
longer matches its frozen digest, the output directory already exists or cannot be created,
or an artifact cannot be written. A partially written directory is evidence of an I/O
failure and is never resumed; choose a new destination after investigating it.

## `run`

Load all generated artifacts through their Pydantic contracts, regenerate the seeded case
and hand-authored packet, re-render both requests, re-prove the sole delta, and then execute
exactly three pairs sequentially.

```text
dsx-context-lift run [OPTIONS] GENERATED_DIRECTORY RUN_ROOT
```

### Arguments

| Name | Type | Required | Meaning |
| --- | --- | --- | --- |
| `GENERATED_DIRECTORY` | path | yes | Directory produced by `generate`. |
| `RUN_ROOT` | path | yes | New private run directory. The path must not exist. |

### Options

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--order-seed` | integer | required | Base seed committed in the manifest and used to derive a deterministic seed for each pair's arm order. |
| `--help` | flag | n/a | Show command help and exit. |

`OPENAI_API_KEY` must be present in the environment. This command makes paid live provider
requests.

```bash
uv run dsx-context-lift run pilot-generated pilot-run --order-seed 731
```

The command derives three deterministic pair IDs and three pair-specific order seeds from
the case digest and base order seed. Attempt IDs are fresh, non-guessable values. It waits
for each request and pair before starting the next one; it does not use concurrency.

Before either blind export or reveal, the CLI binds the terminal run back to this manifest:
there must be exactly the three declared pair summaries, pair identities and seeded arm orders
must match, standalone attempt records and every append-only per-request outcome record must
exactly match their summaries, and every persisted rendered request, request digest, and
common-projection digest must match the committed values.

### Run manifest

`RUN_ROOT/run_manifest.json` is written before client construction or any pair call. Its
`RunManifest` contains:

- case digest, packet version, and exact model identifier;
- generation seed and base order seed;
- intended pair count `3`, deterministic pair IDs, and pair-specific order seeds;
- packet-off request digest, packet-on request digest, and common-projection digest; and
- claim label `one-case unscored information-availability pilot`.

### Pair artifacts

Each `RUN_ROOT/pair-PAIR_NUMBER-PAIR_ID/` contains an exclusive `pair_summary.json` and an
`attempts/` directory. Every attempt directory contains:

| Path | Contract | Timing |
| --- | --- | --- |
| `rendered_requests.json` | `RenderedRequests` | Before the first client call. |
| `attempt_start.json` | `AttemptStart` | Before the first client call. |
| `outcomes/NN-ARM.json` | `ArmOutcome` | Immediately after each request terminates. |
| `attempt_summary.json` | `AttemptSummary` | After the attempt reaches a terminal state. |

An arm retries transport/provider failures at most three requests with 1-second and 2-second
backoffs. If infrastructure exhausts, the whole pair gets one fresh attempt. A twice-exhausted
pair ends as `infra_incomplete`; `run` prints that terminal kind and continues until all three
intended pairs have terminal summaries. Other terminal arm outcomes are `refused`, `incomplete`,
`invalid_output`, or `completed`; they terminate the arm without an infrastructure retry.

### Error conditions

The command exits before client construction if the run root already exists, any generated
artifact is missing/malformed, the persisted case or packet differs from its deterministic
source, a persisted render differs from an immediate re-render, the frozen default digest
fails, or `OPENAI_API_KEY` is absent. It also exits if the run root or manifest cannot be
created. An unexpected client exception preserves all append-only artifacts written before
that exception; the CLI never resumes that root.

## `judge`

Without `--judgments`, export eligible completed decisions to a new public blind bundle.
With `--judgments`, validate and freeze one complete judgment for every opaque output. Freezing
recomputes the public manifest from the verified private run and public blind seed, so its
version, ordered IDs, output digests, and exclusions must all match source evidence. Neither
mode reveals the official arm assignment.

```text
dsx-context-lift judge [OPTIONS] RUN_ROOT BLIND_BUNDLE
```

### Arguments

| Name | Type | Required | Meaning |
| --- | --- | --- | --- |
| `RUN_ROOT` | path | yes | Private source run directory with a valid run manifest. |
| `BLIND_BUNDLE` | path | yes | Public blind directory. New for export; existing for freeze. |

### Options

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--blind-seed` | integer | required | Public seed used for opaque IDs and the blind output permutation. It must match the existing manifest during freeze. |
| `--judgments` | path | omitted | JSON file containing a complete array of `BlindJudgment` objects. Omit to export. |
| `--help` | flag | n/a | Show command help and exit. |

Export example:

```bash
uv run dsx-context-lift judge pilot-run pilot-blind --blind-seed 991
```

The command prints the ordered opaque IDs, a judgment JSON template, and a source-access
warning. The public bundle contains `manifest.json` plus one `OPAQUE_ID.json` per eligible
output. The manifest commits each output digest, records exclusions by non-identifying reason
and count, and exposes no pair ID or official arm mapping.

Do not give evaluators access to `RUN_ROOT`. The blind seed is intentionally public, so an
evaluator who also has the labeled source artifacts can reconstruct treatment labels.

Freeze example:

```bash
uv run dsx-context-lift judge pilot-run pilot-blind --blind-seed 991 \
  --judgments judgments.json
```

`judgments.json` must be a JSON array. Each object has exactly these fields:

| Field | Type or constraint |
| --- | --- |
| `opaque_id` | Non-empty string matching one manifest ID. |
| `decision_quality` | Integer, 1 through 5. |
| `evidence_use` | Integer, 1 through 5. |
| `limitations_quality` | Integer, 1 through 5. |
| `packet_guess` | `packet_off` or `packet_on`; this is an evaluator guess, not an official label. |
| `guess_confidence` | Integer, 1 through 5. |
| `metric_reasoning` | Integer, 1 through 5. |
| `split_strategy` | Integer, 1 through 5. |
| `leakage_row_id_avoidance` | Integer, 1 through 5. |
| `limitations` | Integer, 1 through 5. |
| `overall_recommendation_quality` | Integer, 1 through 5. |
| `exact_prevalence_recognition` | Integer, 1 through 5. |
| `majority_baseline_recognition` | Integer, 1 through 5. |
| `citation_use` | Integer, 1 through 5. |

The array must contain each expected opaque ID exactly once and no extra IDs. Scores must be
JSON integers; numeric strings, floating-point values, and booleans are rejected rather than
coerced. The command
canonicalizes judgments into manifest order, writes `frozen_judgments.json` exclusively, and
prints its path and digest.

### Error conditions

Export fails if the run manifest or source pair ledger is invalid, an opaque ID collides or
is unsafe, any public artifact fails validation, or the destination already exists. Freeze
fails if the bundle/seed is wrong, the JSON or any score is invalid, IDs are missing/duplicate/
extra, a committed output changed, or `frozen_judgments.json` already exists.

## `reveal`

Validate the public outputs and frozen judgments against their digests, reconstruct opaque
IDs from the source run, and publish the official mapping and descriptive report exactly once.

```text
dsx-context-lift reveal [OPTIONS] RUN_ROOT BLIND_BUNDLE
```

### Arguments and options

| Name | Type | Required | Meaning |
| --- | --- | --- | --- |
| `RUN_ROOT` | path | yes | Private source run directory with the committed decisions. |
| `BLIND_BUNDLE` | path | yes | Existing blind bundle with valid frozen judgments. |
| `--help` | flag | n/a | Show command help and exit. |

Example:

```bash
uv run dsx-context-lift reveal pilot-run pilot-blind
```

The command writes `BLIND_BUNDLE/reveal/` exclusively:

| Path | Contract | Content |
| --- | --- | --- |
| `reveal_map.json` | `RevealMap` | Opaque ID to pair number, pair ID, and official arm. |
| `revealed_report.json` | `RevealedReport` | Raw joined judgments and three explicitly separate result sections. |

The terminal output separates `Comparative ratings`, `Packet-uptake diagnostics`, and
`Arm-guess results`. Packet-uptake scores are not folded into the fair comparative ratings.

### Error conditions

Reveal fails before publication if the run manifest is invalid, frozen judgments are absent
or changed, the public/source output commitments differ, or reconstructed opaque IDs differ.
It also fails if `reveal/` already exists. Staged artifacts are validated before their
exclusive publication.

## Related documentation

- [Context Lift](experiments/context-lift.md)
- [Evidence boundary](evidence-boundary.md)
- [Data Access](experiments/data-access.md)
- [Data Access Luna realistic slice](experiments/data-access-luna-realistic.md)
- [Project introduction](../README.md)
