# Data Access

Data Access v2 is the DSX capability-ceiling experiment. It compares three fresh executions of
the same model and task:

| Arm | Receives | Does not receive |
| --- | --- | --- |
| `dsx_packet` | An opaque, committed `dsx_packet` JSON value | A dataset tool |
| `full_data` | A read-only `query_data(sql)` tool over every row in `dataset` | The DSX packet |
| `packet_and_full_data` | The committed `dsx_packet` and the read-only `query_data(sql)` tool | Neither source of information |

The model identifier, prompts, reasoning and service settings, output schema, token limit,
and terminal decision contract are common commitments. The data-bearing arms are not deliberately
cost- or time-equalized with packet delivery: calls, time, failures, tokens, and estimated cost
are measured outcomes.

## Preparing a case

`prepare` takes a case configuration, packet JSON, output directory, model identifier, and a
pricing snapshot. The case configuration is a JSON object with:

```json
{
  "case_id": "pilot",
  "task_prompt": "Recommend a review classifier.",
  "dataset_path": "pilot-case.json",
  "dataset_format": "pilot_case_json",
  "target_column": "label",
  "oracle_version": "v1"
}
```

`dataset_format` is `csv`, `parquet`, or `pilot_case_json`. A pilot-case JSON artifact has a
top-level `rows` array. The prepared DuckDB database retains every source row and column in a
table named `dataset`; there is no framework sampling.

The packet is arbitrary JSON. Preparation canonicalizes it and records its SHA-256 digest and
byte count without interpreting its modules or provenance. Later packet versions may add
tracing fields without changing this experiment contract. Optional packet-build cost or timing
is a separate lifecycle input, not a packet field.

The preparation manifest, run manifest, and objective report carry required, literal v2
version fields. Readers reject missing or unsupported versions rather than interpreting them
as compatible artifacts.

The pricing snapshot commits USD-per-million-token rates for input, cached input, output, and
reasoning buckets, as well as an operator-supplied source and effective date. It produces an
estimate, not an account invoice.

## Discovery safety boundary

The full-data and combined arms can call `query_data` sequentially. Each call permits exactly one `SELECT`,
`WITH`, or `DESCRIBE dataset` statement. The database opens read-only with external access
disabled. `SELECT` and `WITH` discovery must reference the prepared `dataset` relation;
catalog, metadata, settings, and path-disclosure functions (including `duckdb_*`,
`current_setting`, and `getvariable`) are blocked. Mutation, attachment, extension,
external-file/network functions, multiple statements, comments, timeout, and oversized results
are rejected or recorded as typed failed discovery attempts.

Default limits are 12 SQL attempts, 10 seconds per SQL statement, 1,000 rows, and 64 KiB per
result. Results include columns, canonical rows, a stable evidence ID, and a result digest.
The model may aggregate or paginate to inspect all logical rows.

## Evidence and results

Every dataset claim must carry evidence: an RFC 6901 pointer into the committed packet or an
evidence ID from a persisted SQL result. A case oracle classifies claims as `supported`,
`unsupported`, `contradicted`, or `unverifiable`. SQL evidence is replayed against the frozen
database and checked by canonical result digest. Generic packet pointers are artifact-level
reproducible; computation-level replay is available only when a packet tracing adapter supports
it.

Evidence Protocol v1 is deliberately conservative. A packet pointer supports a claim only when
the resolved value equals the asserted value. SQL evidence must query `dataset`, use the
predicate's required aggregate, and return its exact proof shape: `row_count`; `label` plus
`class_count` or `class_rate`; `majority_baseline`; `review_count`; `column` plus `missingness`,
`uniqueness`, or `likely_id`; or one `excluded_column` per recommended exclusion. A reference
that resolves but does not meet these rules remains reproducible evidence while the claim is
classified as unsupported. The protocol and these model-visible conventions are committed in
the arm specifications.

Reports retain total and component elapsed time, model and tool calls, token buckets,
estimated cost, discovery failures, claim classifications, evidence-resolution rates, replay
rates, raw values, summary statistics, and three directional within-repetition contrasts:
`dsx_packet - full_data`, `packet_and_full_data - dsx_packet`, and
`packet_and_full_data - full_data`. Packet-build cost is shown separately with amortized totals
for 1, 10, and 100 reuses for both packet-bearing arms. The efficacy section uses only eligible
final three-arm repetitions; a separate operational section counts every arm run across fresh
repetition retries, including abandoned attempts, infrastructure/provider failures, SQL
failures, tokens, elapsed time, and estimated cost.

## Blind review and claim boundary

`judge` exports completed decisions and claim statements under opaque IDs. The public bundle
does not include arm identity, packet payload, SQL text/results, evidence-source locators,
usage, cost, automatic scores, repetition identity, or source-data references. A complete typed set
of blind judgments must freeze before `reveal` can publish arm labels and combine qualitative
judgments with private objective and operational reports.

Keep the run root private until frozen review. A public blind seed improves reproducibility but
is not a secrecy mechanism for someone with the private run artifacts.

Data Access supports a narrower claim than “DSX is generally better”: on a committed case and
under its declared budgets, packet delivery, full-data discovery, and their combination had the
observed quality, efficiency, consistency, and evidence behavior. The initial one-case run is
descriptive. General claims require multiple representative cases and pre-specified analysis.

## Live contract smoke test

The paid API smoke test is separately gated so the offline development suite cannot trigger it:

```bash
DATA_ACCESS_LIVE=1 OPENAI_API_KEY=... MODEL_ID=... \
  uv run pytest -m live tests/experiments/data_access/test_live.py -q
```

It prepares a tiny deterministic case, executes one real three-arm repetition, and verifies
that every arm leaves typed terminal and append-only request evidence.
