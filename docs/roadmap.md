# DSX harness roadmap

## Executive takeaway

The pilot points to a useful product direction for DSX: help a capable data-science agent
move from a generic methodology to a dataset-specific, inspectable plan.

The packet did not teach the model the basics of imbalanced classification. Both arms chose
the right primary metric and produced reasonable validation strategies. The packet changed
the parts of the answer that depend on knowing the data:

- evidence use increased from 2.0 to 5.0;
- decision quality increased from 4.0 to 5.0;
- explicit row-ID leakage avoidance increased from 4.0 to 5.0; and
- recommendation quality increased from 4.0 to 4.67.

The product implication is more important than the score: **DSX should make relevant dataset
facts, risks, constraints, and unknowns available at the moment a data scientist or agent has
to make a decision.**

The next roadmap should use experiments to discover which packet capabilities are genuinely
useful. The harness should evolve from a fixed pilot runner into a product-discovery platform
that can compare packet modules, delivery formats, discovery workflows, and downstream tasks.

## What the pilot suggests building

### 1. A packet builder, not only a packet contract

The current packet is hand-authored. The first major product feature should be a repeatable
way to create one from an actual dataset.

A packet-builder MVP should accept a CSV or Parquet file plus a target column and produce:

- dataset shape and population summary;
- target counts, rates, and simple baselines;
- column types, missingness, cardinality, and likely identifiers;
- possible target or post-outcome leakage warnings;
- candidate temporal, entity, and group columns;
- review-budget or operating-point calculations;
- unresolved questions that cannot be inferred from the data; and
- evidence references that link every claim to a computation.

This turns the harness from a demonstration of packet delivery into a system that can test
whether DSX-generated packets are useful.

### 2. Modular packets

The frozen Context Lift `Packet` model combines several different things. The reusable
`DsxPacket` envelope now separates them into independently versioned modules:

- descriptive facts;
- column-level risk signals;
- metric guidance;
- split guidance;
- exclusions;
- limitations; and
- evidence references.

These should become independently selectable modules. A modular design lets the harness test
which sections improve a task and lets a product assemble smaller, task-specific packets.

A possible module structure is:

```text
profile
├── population
├── target
├── columns
├── relationships
└── data_quality

decision_support
├── operating_constraint
├── metric_candidates
├── split_constraints
├── feature_risks
└── recommended_exclusions

evidence
├── provenance
├── freshness
├── computation_reference
└── uncertainty

open_questions
├── target_semantics
├── point_in_time_availability
├── entity_structure
└── business_costs
```

The harness can then run an experiment with `profile` only, `profile + feature_risks`, or the
full packet without changing unrelated request fields.

### 3. Concrete risk checks for data scientists

The clearest practical gain in the pilot was naming `row_id` rather than merely warning about
identifiers in general. This suggests a high-value feature category: convert generic good
practice into concrete checks against the current dataset.

Candidate checks include:

- unique and near-unique identifier detection;
- target duplicates and alternate target encodings;
- post-outcome or point-in-time leakage candidates;
- suspiciously predictive columns;
- train/test entity overlap;
- temporal inversion or future information;
- missingness patterns correlated with the target or source;
- unsupported metric choices under class imbalance; and
- requested review budgets that do not match the evaluation cutoff.

Each finding should be typed and machine-checkable. For example, an identifier finding should
record the column, uniqueness rate, evidence reference, severity, and recommended action.

### 4. Packet provenance, freshness, and correction

For a data scientist to trust a packet, every important fact needs context:

- how it was computed;
- which data snapshot and row filter it used;
- when it was computed;
- whether it was observed, inferred, or supplied by a user;
- how certain DSX is; and
- whether a human has confirmed or corrected it.

This implies roadmap features for packet versioning, dataset fingerprints, fact-level
provenance, freshness warnings, and corrections. These are not secondary governance features;
they are part of the core usefulness of generated context.

### 5. Task-specific packet assembly

A single comprehensive profile may be too large and too generic. The useful packet for
choosing a metric is different from the packet for reviewing a split, selecting features, or
planning monitoring.

The harness should test a packet router that selects relevant modules for tasks such as:

- metric and operating-point selection;
- feature review;
- split-strategy review;
- preprocessing design;
- model selection;
- experiment review;
- production-readiness review; and
- drift or incident investigation.

Task-specific assembly could become one of DSX's main differentiators: not “more profile,” but
the smallest useful evidence set for the decision at hand.

### 6. Unknowns and follow-up questions

The current packet mostly states what is known. Real data-science work depends just as much on
surfacing what cannot be established from a file. DSX should distinguish facts from missing
business or semantic context.

Useful generated questions might include:

- What event does the target represent, and when is it observed?
- When must a prediction be available?
- Can multiple rows belong to one entity?
- Which columns exist at scoring time?
- What are the costs of false positives and false negatives?
- Does the 5% review budget apply globally or within segments?
- Are there policy constraints on protected or sensitive fields?

An interactive “resolve packet questions” step may be more valuable than adding more automatic
statistics.

### 7. Decision checks after the agent responds

DSX can create value on both sides of the decision:

```text
dataset → packet → agent decision → decision checks → reviewed plan
```

The pilot already parses structured decisions. The next feature should compare a response
against packet constraints automatically. Examples:

- Did the response exclude every high-risk field?
- Does the primary metric match the operating constraint?
- Does the split strategy respect time and entity structure?
- Are cited packet facts real and current?
- Did the recommendation resolve or acknowledge important unknowns?
- Does the proposed threshold actually select the allowed review volume?

This would make the harness useful as a plan reviewer, not only an experiment runner.

## Experiments to discover useful features

The goal of the next experiments is not merely to prove that packets help. It is to decide
what DSX should build.

### Experiment 1: packet module ablation

Compare several packet configurations on the same task:

1. no packet;
2. descriptive profile only;
3. profile plus feature-risk findings;
4. profile plus operating and metric context;
5. full packet with guidance; and
6. full packet plus unresolved questions.

Measure concrete response behaviors: correct field exclusions, correct metric, correct queue
size, relevant citations, unsupported assumptions, and number of unresolved issues noticed.

**Product decision:** which modules belong in the default packet, which are optional, and
which add noise without changing the decision.

### Experiment 2: generated packet versus hand-authored packet

Run the same cases with:

- the current hand-authored packet;
- a packet generated automatically from the rows;
- an automatically generated packet corrected by a data scientist; and
- an automatically generated packet enriched with task context.

**Product decision:** where automation is already good enough, which facts need human input,
and whether a correction workflow should be central to the product.

### Experiment 3: packet versus equal discovery

Give one agent the packet and another agent access to the rows plus equivalent analysis tools.
Record not only decision quality, but also:

- elapsed time;
- tool calls and failed analysis attempts;
- tokens or compute used;
- facts discovered;
- facts missed;
- reproducibility of the discovery path; and
- whether the final decision can cite its evidence.

**Product decision:** whether DSX's value is better decisions, faster discovery, lower cost,
more consistent evidence, or easier review. This is already identified in `TODOS.md` and
should move to the front of the product-discovery queue.

### Experiment 4: packet size and compression

Create `lite`, `standard`, and `full` packets for the same dataset. Vary the amount of
column-level detail and the number of evidence references.

Test whether the model still notices the important risks and constraints as packet size grows.

**Product decision:** default packet size, prioritization rules, task-specific filtering, and
whether the UI needs expandable detail rather than sending everything to the agent.

### Experiment 5: wrong, stale, and conflicting context

Introduce controlled problems:

- an outdated prevalence;
- a renamed or removed column;
- a false likely-ID flag;
- inconsistent row counts;
- a user assertion that conflicts with a computed fact; and
- a packet from the wrong dataset snapshot.

Observe whether the system detects the inconsistency before the packet reaches the agent and
whether the agent blindly follows bad guidance.

**Product decision:** required provenance fields, validation behavior, staleness policy,
confidence display, and when DSX should block rather than warn.

### Experiment 6: task-specific routing

Use one dataset with multiple tasks: metric selection, feature review, split review, and
deployment review. Compare a full packet with a task-selected packet.

**Product decision:** whether packet routing improves relevance and efficiency enough to
justify a task taxonomy and module-selection engine.

### Experiment 7: static packet versus interactive clarification

Compare:

- a static generated packet;
- a packet that lists open questions;
- a workflow where the user answers those questions before generation; and
- an agent that can request one additional packet module during the task.

**Product decision:** whether DSX should remain a one-shot profiler or become an interactive
context-building workflow.

### Experiment 8: decision validation

Ask models to produce structured analysis plans with deliberate errors: include a likely ID,
use accuracy for a rare target, choose a random split despite repeated entities, or cite a fact
that is not in the packet. Run proposed decision checks against these plans.

**Product decision:** which automated checks are reliable enough to become release gates,
warnings, or reviewer prompts.

### Experiment 9: dataset change and packet diff

Generate packets for two versions of a dataset with changed prevalence, schema, missingness,
or cardinality. Present the agent with either the full new packet or a focused packet diff.

**Product decision:** whether versioned packet diffs help with model refreshes, drift review,
and recurring analysis workflows.

### Experiment 10: real data-scientist workflow trial

Give data scientists a bounded planning or review task with and without DSX. Observe where they
consult the packet, what they distrust, what they correct, and what they still compute
manually.

Measure time to an accepted plan, clarification cycles, concrete errors found, revisions, and
which packet sections were actually used.

**Product decision:** workflow placement, UI priorities, correction mechanisms, and the first
integration target—CLI, notebook, pull-request review, or agent context API.

## Experiment-to-feature map

| Experiment signal | Feature it would justify |
| --- | --- |
| Risk modules drive most improvement | Typed leakage and feature-risk scanner |
| Metric context changes decisions | Operating-constraint and metric recommender |
| Facts help but guidance does not | Facts-first packet with optional guidance |
| Human correction materially improves packets | Packet review and correction workflow |
| Equal discovery is accurate but slow or inconsistent | Cached evidence packets and discovery ledger |
| Lite packets perform as well as full packets | Relevance ranking and token-budgeted packet builder |
| Task-specific packets outperform full profiles | Packet router and task taxonomy |
| Models follow stale or conflicting facts | Hard snapshot validation and provenance gates |
| Open questions prevent bad assumptions | Interactive clarification and unresolved-question model |
| Decision checks catch realistic errors | Automated plan-review command and CI integration |
| Packet diffs improve refresh decisions | Versioned packets and change-impact summaries |
| Users mostly work in notebooks | Notebook extension before a standalone web UI |

## Proposed DSX harness roadmap

### Phase 1 — Turn the fixed pilot into an experiment platform

The current harness is intentionally narrow: one generated case, one packet shape, two arms,
three pairs, and one response schema. The next foundation should make those parts configurable
without weakening the existing evidence ledger.

Build:

- a case registry with task text, dataset source, target, operating constraints, and expected
  checks;
- configurable experiment arms rather than only packet off/on;
- packet modules that can be enabled independently;
- rubric and objective-check versions recorded in the run manifest;
- a generic result summarizer across cases and arms; and
- a small catalog of deterministic synthetic cases for fast experimentation.

The first use of this platform should be the packet-module ablation and equal-discovery
experiment.

**Milestone:** a new experiment can be expressed mostly as configuration rather than new
runner code.

### Phase 2 — Build the packet-generation MVP

Implement a local-first command such as:

```bash
dsx-packet build data.parquet --target label --output dsx-packet.json
```

The MVP should prioritize a few high-value, testable features:

1. shape, target rate, and baseline calculations;
2. column type, missingness, cardinality, and likely-ID detection;
3. review-budget calculations;
4. evidence references and dataset fingerprints;
5. explicit open questions; and
6. human-editable exclusions and constraints.

Avoid a large profiling surface initially. The experiments should earn each additional module.

**Milestone:** DSX can generate a useful packet for several tabular cases without hand-editing
the underlying JSON.

### Phase 3 — Add packet inspection and correction

Create a workflow to inspect what DSX believes before the packet is used:

- readable packet summary;
- fact provenance and computation details;
- warnings and confidence;
- accept, correct, suppress, or annotate a finding;
- packet version and dataset snapshot;
- diff against a previous packet; and
- unresolved questions requiring user input.

This can begin as CLI prompts or an editable YAML/JSON round trip. A richer UI should follow
only if the workflow trial shows it is needed.

**Milestone:** a data scientist can review and correct a generated packet without editing
internal implementation code.

### Phase 4 — Add task-aware delivery and decision checks

Build an API that assembles the smallest relevant packet for a declared task. Add response
validation against packet facts and constraints.

Possible interfaces:

```text
dsx-packet for-task metric-review
dsx-packet for-task feature-review
dsx-packet for-task split-review
dsx-packet check analysis-plan.json --packet dsx-packet.json
```

Provide adapters for agent prompts, notebooks, and CI or pull-request checks only after the
core packet and validation contracts stabilize.

**Milestone:** DSX supports an end-to-end loop from dataset to packet to decision to automated
review.

### Phase 5 — Validate workflow value and choose the product surface

Run the real data-scientist workflow trial before committing to a large interface. Use the
observed workflow to choose among:

- CLI and files for reproducible pipelines;
- Python and notebook APIs for exploratory work;
- an agent-context service for automated assistants;
- CI checks for model-plan review; or
- a web interface for collaborative packet review.

The likely product may combine several surfaces, but the first one should be selected from
observed usage rather than preference.

**Milestone:** one workflow shows repeatable user value and defines the primary integration
surface.

## Recommended next three roadmap increments

### Increment 1: configurable packet ablation

Refactor the current packet into modules and allow an experiment to define more than two
arms. Add objective response checks for exact prevalence, queue size, excluded columns,
metric choice, and citation validity.

This is the fastest way to turn the pilot into feature evidence.

### Increment 2: local packet-builder prototype

The first history-aware builder is available as `dsx-packet build`. It profiles a CSV or Parquet
file, optionally consumes a declared transformation manifest, and writes an immutable packet
bundle with dataset, column-cardinality, target, history, data-trap, and feature-risk modules.
Exact likely-identifier detection is implemented on that builder: a fully populated non-target
column with `distinct_count == row_count` is reported as a warning-only `likely_identifier`
finding.

Further builder work can still add near-unique identifier detection, semantic leakage checks,
review-budget calculations, and open questions, then use module-ablation cases to assess whether
generated packets preserve the useful parts of a hand-authored packet.

### Increment 3: equal-discovery and workflow experiments

Run packet delivery against an agent with equivalent row and tool access. In parallel, put the
packet-builder prototype in front of a small number of data scientists and observe what they
use or correct.

Together these experiments answer two roadmap questions:

1. What value comes from packaging evidence rather than discovering it on demand?
2. Which DSX features fit naturally into a real data-science workflow?

## Backlog candidates by priority

### Build now

- packet modules and experiment-arm configuration;
- objective structured-response checks;
- local CSV/Parquet packet builder;
- likely-ID and basic leakage-risk findings;
- dataset fingerprints and fact provenance;
- open questions in the packet contract; and
- a generic experiment summary.

### Build after the first feature experiments

- task-specific packet routing;
- packet-size optimization;
- interactive correction workflow;
- packet diff and freshness checks;
- decision-validation command;
- notebook or agent-context adapter; and
- discovery cost and tool-use ledger.

### Defer until workflow demand is clear

- a large standalone dashboard;
- broad provider and model integrations;
- real-time collaborative packet editing;
- organization-wide policy management;
- hosted dataset storage;
- automated model training; and
- production monitoring infrastructure.

These may become valuable, but the pilot does not yet tell us which product surface users
will adopt.

## Product direction

A useful near-term description of DSX harness is:

> DSX harness is an experimentation and delivery layer for dataset context. It discovers
> which facts, risks, constraints, and questions improve data-science decisions; packages the
> useful ones into task-specific evidence; and checks whether downstream plans respect them.

The harness should remain the place where new DSX features earn their way into the product.
Every proposed packet module or workflow feature should first answer a concrete question in
an experiment, demonstrate a useful behavior, and only then become part of the default DSX
experience.
