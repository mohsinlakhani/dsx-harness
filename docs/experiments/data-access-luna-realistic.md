# Data Access: Luna realistic slice

This is a **new Data Access study identity**. It reuses the Data Access v2
runner (`prepare`, `run`, `judge`, `reveal`) plus freeze, suite, and post-reveal
uptake helpers. It is not a third harness and not a reinterpretation of the
2026-08-27 Terra pilot. Terra artifacts stay frozen and are not pooled with
these results.

**Question.** On three frozen DataSciBench tables, with Luna and the Data Access
planning/review contract held fixed, how do generated DSX Packets compare with
on-demand SQL discovery and with both, on quality, evidence, uptake of
identifier and imbalance facts, time, and cost?

Datasets only are inherited from DataSciBench. Do not import original prompts,
TFC tests, or leaderboard scoring.

## Gated source data

Hugging Face dataset `zd21/DataSciBench` is gated. Accept the license, download
candidate tabular files, and **freeze only after those files are readable
locally**. `case_id`s are assigned after download, not from a catalog of unread
names.

## Eligibility

Every frozen case must satisfy all of the following:

- Tabular CSV or Parquet.
- One defensible classification target in the file. Binary is preferred. If
  multi-class, the target must have at most 10 distinct non-null values.
- `dsx-packet build` succeeds on the frozen copy. Freeze has no `--manifest`
  option; do not invent transformation history for imported DataSciBench files.
- At least one eligibility signal, measured on the committed builder packet:
  - `feature-risks` contains at least one `likely_identifier` finding; or
  - `data-traps` contains `target_class_imbalance` (builder threshold: minority /
    majority class count `< 0.20`, or a single class); or
  - `dataset-profile` has at least one column with `missing_rate >= 0.05`.
- Fits the current `prepare` path, which materializes and digests every row
  with no sampling: at most 20,000 rows and 40 columns. The 12 SQL attempts,
  1,000-row cap, and 64 KiB cap must still allow aggregation over the full
  table. Reject anything larger rather than changing materialization.
- Usable license. No images, visualization-only dumps, or tables with no target.

Prefer complementary stresses across the three tables (identifier-heavy,
imbalanced target, material missingness). If the licensed slice cannot supply
all three, keep the eligibility bar and record which stresses are missing.

**Stop if fewer than three tables pass eligibility.** Do not fill with the
synthetic Terra/pilot case or a hand-authored packet. Stop and either select
another licensed file or shrink the study claim.

## Shared task

All three cases use the same planning/review prompt and response contract.
Only `case_id`, dataset path and digest, `target_column`, and the generated
packet differ. Copy this prompt exactly:

```text
Recommend a classifier for a 5% manual-review budget. Return the required structured decision. Use factual_claims only for facts you can cite exactly.
```

The freeze helper writes this string into `case.json`. Do not edit it per case.

## Commands

Destinations under gitignored `artifacts/data-access-luna-realistic/` are
exclusive. Run these in order after the local files exist.

### 1. Freeze each eligible table

```text
uv run dsx-data-access freeze DATA.csv artifacts/data-access-luna-realistic/CASE/freeze \
  --case-id CASE --target TARGET --source-id datascibench:ID --license-accepted
```

`--license-accepted` is required. Optional `--packet-id ID` defaults to the
case id. Freeze copies the dataset, times `dsx-packet build`, gates on
review-classifier task assembly, and writes:

- the local dataset copy (SHA-256 recorded in the freeze note)
- `case.json` (`oracle_version: v1`, shared task prompt above)
- `packet.json` and `build-record.json`
- `packet-build-metrics.json` from the timed build (elapsed seconds; estimated
  packet-build cost is zero for local builds)
- `freeze-note.json`: DataSciBench source id, license acceptance, dataset-only
  inheritance, packet digest, module IDs, and which eligibility signals fired

The live payload is the full builder `packet.json`. Default modules are
`dataset-profile`, `column-profile`, `target-profile`, `data-traps`, and
`feature-risks`, in that order. `transformation-history` is expected to be
absent for this slice.

### 2. Suite prepare and run

Commit one Luna identifier and a pricing snapshot. **Stop before live calls if
that identifier is not callable with `OPENAI_API_KEY` + `MODEL_ID` on the
existing OpenAI Responses path used by `dsx-data-access`.** Do not splice
Cursor-chat transcripts into the ledger.

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

```text
uv run dsx-data-access suite suite.json artifacts/data-access-luna-realistic/suite
```

`suite` sequences existing `prepare` and `run` over the freeze directories and
writes a pooled `suite-index.json`. It does not introduce a second scoring
language. If one case fails, the others keep their artifacts; the pooled index
marks the failed case ineligible. Default limits stay at three three-arm
repetitions, 16 model calls, 12 SQL attempts, 4,096 output tokens, a 10-minute
arm cap, a 10-second SQL timeout, and 1,000 rows / 64 KiB per result.

Scale: **three cases × three three-arm repetitions = 27** intended live outputs.
Cases are never mixed in one run manifest.

### 3. Per-case blind review, then uptake

`judge` and `reveal` remain per-case commands. Use **one frozen blind bundle
per case**. Do not encode arm identity in public IDs.

```text
uv run dsx-data-access judge RUN_ROOT BLIND --blind-seed SEED
uv run dsx-data-access judge RUN_ROOT BLIND --blind-seed SEED --judgments judgments.json
uv run dsx-data-access reveal RUN_ROOT BLIND
uv run dsx-data-access uptake RUN_ROOT artifacts/data-access-luna-realistic/CASE/uptake
```

Uptake is a post-reveal diagnostic. It must not appear in the public blind
bundle. `dsx-data-access uptake` does **not** call `validate_run_root`; it
expects a completed run root after `run` (typically after reveal). Independent
use on a mutated or pruned root is unsupported.

After freeze, for each completed output it records identifier exclusion,
imbalance acknowledgement when `target_class_imbalance` is present, and whether
packet-bearing arms cited `column-profile`, `feature-risks`, or `data-traps`.
Identifier `all_excluded` is only meaningful when identifier `columns` is
non-empty / `applicable` is true. Imbalance acknowledgement is the substring
`"imbalance"` in the lowercase concatenation of reasoning, recommendation, and
limitations.

Keep each case run root private until that case's judgments are frozen. Write
`docs/experiments/data-access-luna-realistic-results.md` from the revealed
reports; do not pool Terra scores.

## Allowed claim

On these three frozen tables, under these limits, with this generated packet
and this Luna identifier: how packet delivery, SQL discovery, and both compared
on quality, evidence, uptake of identifier and imbalance facts, time, and cost.

**Not claimed:** general DSX superiority, DataSciBench leaderboard lift, or that
the builder matches a hand-authored packet.

### Pre-registered stop/go

- If packet-only matches combined quality at lower cost on at least two cases,
  packaging is earning its keep on real tables.
- If SQL-only dominates quality and the packet is unused or wrong, the next
  experiment is the builder (or packet content), not another three-arm idea.
- If all arms collapse, the review-classifier recast was a bad fit for those
  files.

## Operator inputs

Record these at freeze/prepare; they are not protocol TBDs:

- Hugging Face access and the three source file ids
- Exact Luna model identifier string
- Pricing snapshot for that identifier
- Order seeds per case
- Judge identity for the 27-output blind pass

## Operator work remaining

1. Accept Hugging Face terms and download candidate tabular files.
2. Freeze until three cases pass; stop rather than filling with the synthetic
   pilot.
3. Commit pricing snapshot and Luna `MODEL_ID`.
4. Run the suite, three per-case blind reviews, reveal, then uptake.
5. Write `docs/experiments/data-access-luna-realistic-results.md` from the
   revealed reports.
