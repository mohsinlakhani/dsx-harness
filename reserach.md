# Research and product ideas for DSX

_Last updated: 2026-09-02_

## Purpose

This is a working research note for features that may be worth testing in DSX. It connects
three related ideas:

1. grounding an agent in a live, typed platform instead of asking it to emit disposable code;
2. making experimentation components reusable and directly contributable by data scientists;
   and
3. evaluating data-science work with task-specific executable checks rather than a single
   subjective score.

The strongest combined direction is:

> DSX should become a grounded experimentation and evaluation substrate for data-science
> agents: experiments are persistent typed artifacts, capabilities and metrics are discoverable
> from registries, mutations are validated before commitment, and outputs are assessed by a mix
> of reproducible programmatic checks and blind human review.

This extends the repository's current strengths—immutable evidence, DSX Packets, task
projections, controlled arms, and blind evaluation—without requiring DSX to become a general
workflow orchestrator immediately.

## 1. DataFlow-Harness

### Source

- [Paper: DataFlow-Harness: A Grounded Code-Agent Platform for Constructing Editable LLM Data Pipelines](https://arxiv.org/abs/2607.16617)
- [HTML paper](https://arxiv.org/html/2607.16617v2)
- [DataFlow WebUI source](https://github.com/OpenDCAI/DataFlow-WebUI)
- [DataFlow documentation](https://opendcai.github.io/DataFlow-Doc/)

### Summary

The paper names an **NL2Pipeline gap**: coding agents can generate executable scripts, but
those scripts are often detached from the host platform and do not become persistent,
inspectable, editable, or governable workflow artifacts.

DataFlow-Harness closes that gap with four cooperating pieces:

- a pipeline backend that is the authoritative state store;
- a live operator registry and pipeline state exposed to the agent through MCP;
- reusable Skills that encode procedural knowledge and composition guidance; and
- a conversational UI synchronized with a visual DAG editor.

The important mechanism is a **Request → Validate → Commit** protocol. The agent does not write
an unconstrained workflow blob. It applies typed incremental mutations—add an operator, update
parameters, connect nodes—to the current pipeline. A mutation is committed only when the graph
remains acyclic and adjacent schemas are compatible. Both human edits and agent edits operate
on the same persistent representation.

On the paper's 12-task benchmark, run ten times per task, the full harness reported a 93.3%
end-to-end pass rate. That was close to the 94.2% context-aware script baseline and above the
83.3% MCP-only configuration. Compared with vanilla script generation, the harness reported
72.5% lower monetary cost and 49.9% lower generation latency. The ablation is especially
relevant: procedural Skills helped most on tasks where correct construction depended on
implicit domain knowledge; they added little to trivial routing tasks and did not fix failures
caused by the underlying model or downstream output quality.

### What DSX should borrow

- **One authoritative artifact:** a study, case, run, and evaluation should be views over a
  single persisted object graph, not loosely related CLI outputs.
- **Live grounding:** an agent should query the exact registered case types, arms, packet
  modules, tools, and evaluators available in the installed DSX version.
- **Typed incremental change:** modifying an experiment should mean changing a typed field or
  node, with validation, rather than regenerating a runner.
- **Procedural guidance separate from capability:** tool schemas describe what is possible;
  DSX playbooks describe how to build a sound comparison, preserve blinding, or select checks.
- **Local and visual editing over the same state:** a future UI should not create a second
  experiment format.

### Caveats

The evidence is promising but narrow. The benchmark contains 12 workflow-construction tasks,
uses one underlying model, and benefits from DataFlow's existing operator ecosystem. Structural
and schema validation do not guarantee semantic correctness, available endpoints, or useful
outputs. DSX should therefore test these ideas with its own cases rather than treating the
reported pass rate as a transferable result.

## 2. How Netflix structures experimentation

### Most likely post you remembered

- [Reimagining Experimentation Analysis at Netflix (2019)](https://netflixtechblog.com/reimagining-experimentation-analysis-at-netflix-71356393af21)
- [Engineering for a Science-Centric Experimentation Platform (paper)](https://arxiv.org/abs/1910.03878)

Netflix describes a modular, science-centric analysis platform in three tracks:

```text
metric definition (Metrics Repo)
        → statistical analysis (Causal Models)
        → rendering (XP Viz / Plotly)
        → experiment UI (ABlaze)
```

This is the harness-like structure: data scientists can contribute metrics, statistical
methods, and visualizations in familiar languages instead of handing every idea to an
engineering team for productionization. Local notebook analysis and production analysis use
the same codebase, so a locally reproduced result can be promoted through a pull request.
The interfaces hide distribution and optimization details: an author implements a small
statistical comparison while the platform handles data access, compression, and parallelism.

### The current public picture

- [DataJunction as Netflix's answer to the missing piece of the modern data stack (2026)](https://netflixtechblog.com/datajunction-as-netflixs-answer-to-the-missing-piece-of-the-modern-data-stack-92af926b40a5)
- [DataJunction source](https://github.com/DataJunction/dj)
- [Sequential A/B Testing Keeps the World Streaming Netflix, part 1 (2024)](https://netflixtechblog.com/sequential-a-b-testing-keeps-the-world-streaming-netflix-part-1-continuous-data-cba6c7ed49df)

The 2026 update shows the metric layer evolving toward **DataJunction**, an API-first semantic
layer. Metrics and dimensions live in a connected graph, can be discovered and traced back to
their definitions, and can be served to multiple clients through a stable interface. Netflix
says this addresses a recurring experimentation problem: scattered definitions, tribal
knowledge, and metric onboarding that could take weeks. The intended result is portable,
inspectable, accountable metrics rather than definitions embedded inside pipelines or
dashboards.

Netflix also uses sequential, anytime-valid inference for software canaries. This permits
continuous monitoring and early stopping while controlling false-alarm probability, avoiding
the invalid repeated “peeking” associated with fixed-horizon tests. That idea is relevant to a
future DSX mode that evaluates rolling model or pipeline changes, but it is not needed for the
current offline packet studies.

These posts are public snapshots, not a complete specification of Netflix's internal platform
today. The durable design lessons are more useful than copying the implementation:

- separate metric definitions, statistical methods, and presentation;
- let domain experts contribute through small stable interfaces;
- keep local reproduction and production execution on the same path;
- centralize semantics and provenance rather than duplicating metrics per experiment; and
- treat advanced methods as composable plugins over shared data contracts.

### What DSX should borrow

- A **metric/evidence registry** that owns definitions, parameters, expected inputs, output
  types, provenance, and versioning.
- A small **evaluator SDK** in which contributors implement a check without understanding run
  storage, blind labels, retries, or report assembly.
- A separation between evidence extraction, comparison/statistics, and report rendering.
- Reproducible local execution that is identical to CI or a future service execution path.
- Deep links from every reported number or claim to the defining evaluator and source
  evidence.

## 3. DataSciBench

### Source

- [Project site](https://datascibench.github.io/)
- [Paper](https://arxiv.org/abs/2502.13897)
- [Code](https://github.com/THUDM/DataSciBench)
- [Evaluation data](https://huggingface.co/datasets/zd21/DataSciBench)

### Summary

DataSciBench evaluates agents on realistic, multi-step data-science tasks rather than only
code snippets with simple exact answers. It defines six broad task types:

1. data cleaning and preprocessing;
2. data exploration and statistical understanding;
3. data visualization;
4. predictive modeling;
5. data mining and pattern recognition; and
6. interpretability and report generation.

Its central contribution is the **Task–Function–Code (TFC)** framework. A prompt is decomposed
into important tasks; each task is paired with an evaluation function and executable checking
code. The released benchmark contains 222 prompts, 519 test cases, and 25 aggregated evaluation
functions. Ground truth was created semi-automatically using repeated model generations,
self-consistency, existing tests where available, and human verification.

The useful idea for DSX is not the historical model leaderboard. It is the evaluation shape:
one data-science request may have several independently checkable obligations, and each
obligation can select the evaluator appropriate to its output. This avoids reducing success to
“the script ran” or relying exclusively on an opaque LLM judge.

### What DSX should borrow

- A task taxonomy that maps naturally to DSX Packet modules and evaluator families.
- A typed `TaskCheck` contract similar to TFC: task/claim, evaluator identifier and version,
  required artifacts, parameters or threshold, and structured result.
- Multiple checks per case, preserving check-level results rather than only an aggregate score.
- A benchmark import format containing prompt, data references, expected output artifacts,
  checks, and provenance.
- Human verification for subjective or uncertain ground truth, while keeping deterministic
  checks for shapes, values, files, schema, citations, leakage exclusions, and metric choices.

### Caveats

Many DataSciBench checks ultimately become Boolean or thresholded outcomes, which can hide how
far an answer missed and can make the aggregate sensitive to the chosen threshold. Some ground
truth and evaluator code is model-assisted. DSX should retain raw measurements, evaluator
diagnostics, and provenance, and should use blind human review when semantic quality cannot be
captured safely in code.

## 4. Combined design for DSX

The three sources converge on the same architecture:

```text
declarative study intent
        ↓
live registries: cases · packet modules · arms · tools · metrics · evaluators
        ↓
typed Request → Validate → Commit mutations
        ↓
persistent study/run artifact graph
        ↓
execution with immutable evidence and local/CI parity
        ↓
task-specific programmatic checks + blind human review
        ↓
traceable report and artifact diff
```

The distinction from DataFlow is important: DSX does not yet need to construct arbitrary data
pipelines. Its native artifact should first be an **experiment graph**—case inputs, dataset
snapshot, packet projection, arms, executions, decisions, claims, checks, judgments, and
report. Builders and analysis operators can become nodes later if experiments demonstrate the
need.

## 5. Potential features, prioritized

| Priority | Feature | DSX adaptation | Evidence that should earn it |
| --- | --- | --- | --- |
| P0 | Declarative `ExperimentSpec` | Configure cases, arms, repetitions, ordering, tools, response contracts, evaluators, and reveal policy without a new runner package. Compile to the existing immutable ledger. | Reproduce Context Lift and Data Access from specs with no evidence-boundary regression. |
| P0 | Evaluator registry and SDK | Versioned objective checks with typed inputs/results; the framework supplies artifact loading, blind-label protection, logging, and aggregation. | Implement existing citation, field-exclusion, metric-choice, and request-equality checks through one interface. |
| P0 | Case/benchmark registry | Versioned cases with dataset refs, task taxonomy, expected artifacts, applicable checks, and licenses/provenance. | Run a small multi-case suite and report per-task confidence intervals rather than a one-case narrative. |
| P0 | Experiment validation protocol | Validate references, arm deltas, data access, check compatibility, blinding, and graph integrity before committing a spec or starting a paid run. | Mutation/fuzz tests show invalid or label-leaking studies are rejected before provider calls. |
| P1 | Artifact/run graph | Address every derived artifact by type, digest, producer, inputs, and version; expose lineage from final report back to dataset and request. | Every reported claim can be traversed and replayed from committed inputs. |
| P1 | Live capability service | Read-only CLI/Python/tool interface for listing available packet modules, cases, SQL tools, evaluators, and current run state. Mutation endpoints come only after validation is stable. | An agent can assemble a valid study without repository-wide code reading and with lower token/cost overhead. |
| P1 | Metric and evidence semantic registry | Canonical definitions for prevalence, queue size, evidence use, decision quality, latency, cost, etc.; include dimensions, SQL/computation, ownership, and versions. | The same metric definition is reused across experiments and local recomputation matches reports. |
| P1 | Packet and experiment diffs | Semantic diff for packet versions, study specs, run inputs, metrics, and evaluator versions—not only JSON text. | Reviewers find intentional and accidental study changes faster and more accurately. |
| P1 | Contribution workflow | Small interfaces for a packet builder, tool, metric, statistical method, evaluator, or renderer; one test harness promotes local contributions into standard runs. | A new evaluator or packet module can be added without changing orchestration code. |
| P2 | Visual experiment editor | Read/edit the same experiment graph used by CLI and agents; display lineage, validation failures, arm deltas, and blind/reveal state. | Workflow trials show that visualization improves review or correction enough to justify UI cost. |
| P2 | DataSciBench adapter | Import a licensed subset of cases into DSX's case/check contracts; do not fork the upstream benchmark silently. | The adapter reproduces upstream checks and identifies where DSX-specific packet features change outcomes. |
| P2 | Sequential/canary evaluation | Anytime-valid monitoring and explicit stopping rules for recurring production comparisons. | DSX acquires a genuine streaming or rollout use case; offline fixed studies should stay simpler. |

## 6. Recommended next implementation sequence

### Increment 1: generalize the experiment contract

Introduce typed `ExperimentSpec`, `CaseSpec`, `ArmSpec`, and `EvaluatorSpec` models. Add a
compiler that produces the same frozen inputs and manifests used by the existing experiments.
Keep the current experiment packages readable and reproducible; use them as golden fixtures.

### Increment 2: make evaluation composable

Extract objective checks behind a versioned evaluator interface. Preserve both raw
measurements and pass/fail policy. A result should identify its evaluator code/version, exact
inputs, parameters, output, diagnostics, and content digest. Keep comparative human scoring
and packet-uptake diagnostics separate, as the current evidence boundary already requires.

### Increment 3: add a multi-case benchmark slice

Create several deterministic tabular cases spanning preprocessing, exploration, predictive
planning, leakage review, and report interpretation. Borrow DataSciBench's task decomposition,
but choose cases that specifically test DSX's thesis: whether grounded packet evidence improves
dataset-specific decisions. Run packet-module and Skills ablations, not only model comparisons.

### Increment 4: expose live discovery

Provide a read-only capability registry through Python/CLI and, if agent integration warrants
it, a small MCP server. An agent should be able to ask what is installed, inspect exact schemas,
retrieve current experiment state, and validate a proposed spec. Only then add typed mutation
and validated commitment.

### Increment 5: add semantic metrics and lineage

Unify metric definitions and attach every report value to its computation and upstream
artifacts. This is the DSX-scale version of the Netflix Metrics Repo/DataJunction lesson and a
natural extension of the current packet evidence-reference work.

## 7. Experiments to run before larger product investment

1. **Spec versus bespoke runner:** implement the same new study declaratively and with the
   current package-per-experiment pattern. Compare implementation effort, validation failures,
   diffability, and reproducibility.
2. **Evaluator coverage:** annotate a small suite with task-level checks. Measure what fraction
   is captured deterministically, what needs statistical comparison, and what still requires
   blind human judgment.
3. **Grounding ablation:** compare repository context, registry/tools only, and
   registry/tools plus procedural DSX guidance. Track correctness, tool calls, tokens, latency,
   and invalid proposed mutations.
4. **Metric portability:** reuse one canonical metric across two experiments and a local
   notebook/CLI calculation. Confirm identical definitions and results.
5. **Artifact review study:** compare JSON/file review with a generated lineage view before
   building an interactive editor.

## 8. Things not to build yet

- A general-purpose DAG execution engine: reuse DuckDB and existing runners until DSX needs
  reusable builder/operator graphs, then integrate or adopt an engine rather than cloning one.
- A large web UI: first prove that artifact lineage and typed editing improve the workflow.
- Fully automated semantic grading: keep human review where intent, usefulness, or uncertainty
  cannot be represented by robust executable checks.
- Organization-wide governance and hosted data storage: these are different product bets from
  a local-first research harness.
- Sequential inference for ordinary offline trials: use it only when observations genuinely
  arrive over time and stopping decisions are part of the protocol.

## Bottom line

The near-term opportunity is not “add more agents.” It is to make DSX itself a better-defined
environment for agents and researchers: a declarative experiment model, live typed registries,
validated mutations, persistent lineage, reusable metric/evaluator components, and a broader
task-level benchmark. DataFlow-Harness supplies the grounded artifact pattern, Netflix supplies
the contribution and semantic-metric pattern, and DataSciBench supplies the executable
evaluation pattern.
