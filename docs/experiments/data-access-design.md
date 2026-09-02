# Data Access experiment design

## Summary

The existing packet-on versus packet-off information-availability pilot is named **Context
Lift**. The second experiment is named **Data Access** and Data Access v2 compares the same
model in three conditions:

- `dsx_packet`: receives an opaque DSX packet and no data-query tool;
- `full_data`: receives no packet and has read-only SQL access to every dataset row.
- `packet_and_full_data`: receives the same opaque packet and read-only SQL access to every
  dataset row.

Data Access is a capability-ceiling study. Discovery receives generous fixed limits and its
resource consumption is measured rather than equalized with the packet-bearing arms. The existing
Context Lift implementation and persisted contracts remain frozen.

## Framework and interfaces

Add a separate `dsx.experiments.data_access` package and `dsx-data-access` CLI with `prepare`,
`run`, `judge`, and `reveal` commands.

`prepare` accepts a reusable JSON case configuration, a dataset, arbitrary `dsx_packet` JSON,
an exact model configuration, a frozen pricing snapshot, and optional packet-build metrics.
It materializes CSV, Parquet, or pilot-case JSON into a DuckDB table named `dataset`, then
commits source-data, materialized-data, packet, task, tool-schema, oracle, pricing, and
configuration digests in immutable artifacts.

The packet payload is canonicalized, digested, and delivered unchanged. Packet validation and
tracing stay outside its schema through a `PacketTraceAdapter`. The initial adapter resolves
RFC 6901 JSON pointers; future adapters can replay richer packet provenance without changing
experiment contracts.

All three arms share the exact model, service tier, system and task prompts, reasoning settings,
structured-output schema, output-token limit, and terminal decision contract. The fairness
validator permits differences only in packet context, dataset descriptor, and tool
availability. Arm order is seeded and randomized within each three-arm repetition, and every arm
starts from fresh model state.

Default committed capability limits are three three-arm repetitions, 16 model calls per arm, 12
SQL attempts, 4,096 output tokens per model call, a 10-minute arm limit, a 10-second SQL
timeout, and 1,000 rows or 64 KiB per tool result. Tools execute sequentially.

## Full-data discovery and execution ledger

Expose one custom Responses API function, `query_data(sql)`, backed by a prepared DuckDB
database opened read-only with external access disabled. Permit one `SELECT`, `WITH`, or
`DESCRIBE dataset` statement. Reject mutations, attachments, extensions, external-file and
network functions, multiple statements, timeouts, and oversized results. The model can query
or paginate all rows; the framework does not sample.

Persist every SQL attempt with its text, typed outcome, duration, canonical result, result
digest, and stable evidence ID. Syntax, policy, execution, timeout, invalid-argument, and
result-size failures count as failed discovery attempts. Provider and transport failures
remain separate infrastructure metrics.

Use a stateless Responses API loop with `store=False`. Persist raw envelopes, continuation
items, function calls, function results, timing, and usage append-only before continuing.
Use fresh-repetition retries for exhausted infrastructure failures: when any arm exhausts its
infrastructure retries, restart the complete three-arm repetition once.

## Claims, evidence, and metrics

The Data Access decision includes `factual_claims`. Each claim has a stable ID and statement,
a case-defined predicate, arguments and asserted value, and packet-pointer or tool-result
evidence references. Narrative fields refer to claim IDs when relying on dataset facts.

A pluggable case oracle classifies each registered claim as:

- `supported`: correct with deterministically supporting evidence;
- `unsupported`: correct but missing, invalid, or irrelevant evidence;
- `contradicted`: conflicts with the dataset oracle;
- `unverifiable`: outside that oracle.

The first registered oracle covers the synthetic pilot case: row and class counts, rates,
majority baseline, review count, missingness, uniqueness, likely-ID status, and recommended
exclusions.

Track per call, arm, repetition, case, and experiment:

- total elapsed time plus model and SQL latency;
- model and tool calls;
- successful and failed discovery attempts by category;
- input, cached-input, output, reasoning, and total tokens;
- estimated cost from the committed pricing snapshot;
- supported, unsupported, contradicted, and unverifiable claims;
- invalid evidence references and evidence-resolution rate;
- cited SQL replay count, digest matches, and replay rate;
- packet-pointer resolution and optional computation replay.

Report inference cost, supplied packet-build cost, and amortized DSX cost at 1, 10, and 100
reuses separately. Re-run cited successful SQL against the frozen database and compare result
digests. Generic packet evidence provides artifact-level reproducibility; computation replay
is `not_applicable` until a compatible trace adapter exists.

Reports contain raw records, three arm means and medians, and directional within-repetition
contrasts for `dsx_packet - full_data`, `packet_and_full_data - dsx_packet`, and
`packet_and_full_data - full_data`. Packet-build and amortized cost fields apply to both
packet-bearing arms. Initial one-case, three-repetition results remain descriptive.

## Blind evaluation, verification, and documentation

Use Data Access-specific mask/freeze/reveal contracts. Public blind outputs include decisions
and claim statements but exclude arm identity, packet content, SQL, evidence-source locators,
usage, and automatic scores. Reveal joins blind quality judgments with claim, timing, call,
usage, cost, failure, and reproducibility metrics.

Acceptance requires:

- unchanged Context Lift behavior and passing legacy tests;
- opaque packet round-tripping despite new modules or tracing fields;
- query access to every source row and column with safe rejection of prohibited SQL;
- complete multi-turn usage, cost, failure, claim, and replay ledgers;
- fairness validation and append-only retry evidence;
- blind exports free of private data and operational metadata;
- 100% statement and branch coverage for both experiment packages;
- clean Ruff, strict mypy, offline tests, and package build;
- an opt-in live smoke test for one Data Access three-arm repetition.

Update the README and experiment documentation to use **Context Lift** and **Data Access**.
After the framework is complete, replace the equal-discovery TODO with a follow-up to run Data
Access on multiple representative cases.
