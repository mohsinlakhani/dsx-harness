# TODOS

## Evaluation

### Run Data Access across representative datasets

**Status:** In progress — tooling for the Luna realistic slice is in this branch; live runs wait on gated DataSciBench downloads and a callable Luna id. Protocol: [Data Access Luna realistic slice](docs/experiments/data-access-luna-realistic.md).

**What:** Run the completed Data Access framework across multiple representative datasets and packet versions.

**Why:** One synthetic case can describe packet-only, data-only, and combined-access behavior but cannot establish a general DSX advantage.

**Context:** Data Access holds the model and task contract constant across packet-only, full-data, and combined arms; data-bearing arms have full logical access through read-only SQL. It tracks evidence, cost, timing, and failed queries. Pre-register case selection and analyze within-repetition contrasts descriptively before broadening claims. The first follow-on study is three frozen DataSciBench tables with builder-generated packets and Luna; Terra results are not pooled.

**Effort:** M
**Priority:** P2
**Depends on:** Hugging Face access to `zd21/DataSciBench`, three eligible frozen tables, Luna `MODEL_ID`, and a pricing snapshot

## Completed
