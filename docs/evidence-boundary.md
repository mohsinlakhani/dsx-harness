# Evidence boundary

This document describes **Context Lift**, the frozen packet-on versus packet-off pilot. Its
separate successor, **Data Access**, compares packet delivery with a full-data discovery arm;
see [Data Access](experiments/data-access.md). The Context Lift evidence and persisted contracts are not
reused or reinterpreted as Data Access results.

This pilot asks a narrow question: for one deterministic synthetic case, how do model
decisions differ when a hand-authored evidence packet is available versus absent? It does
not establish that DSX is generally superior, and it is not a causal proof against a capable
agent that can inspect the underlying rows and independently discover the same facts.

## The failure mode

A packet comparison is uninterpretable if the two requests also differ in model, prompt,
schema, task wording, or hidden execution behavior. A blind score is likewise
uninterpretable if treatment labels become available before judgments are immutable.

The harness therefore treats request equality, execution history, and evaluation timing as
evidence that must be committed on disk rather than assumptions held by the operator.

## The sole-delta proof

`generate` renders two complete requests:

```text
packet_off.context.profile_packet = null
packet_on.context.profile_packet  = <hand-authored Packet>
```

It removes only `context.profile_packet` from each canonical JSON request and compares the
remaining projections. The model identifier, system prompt, task prompt, response schema
name, context structure, and any future non-packet fields must match. Both common projections
must also have the same SHA-256 digest. A missing packet path is not equivalent to an explicit
`null`; that structural drift fails the proof.

```text
                         full packet-off request
                                  |
                                  | remove allowlisted packet path
                                  v
                            common projection
                                  ^
                                  | remove allowlisted packet path
                                  |
                          full packet-on request
```

Canonical request JSON is sorted, compact UTF-8 JSON and rejects non-finite floats such as
`NaN` or `Infinity`. This prevents digests from committing non-standard JSON that a provider
might parse differently.

`run` does not trust the earlier render blindly. It loads the case, packet, configuration,
and rendered requests through Pydantic; regenerates the seeded case and candidate packet;
and re-renders and re-proves the requests immediately before the live client boundary. A
changed deterministic case or packet, or a mismatch between the persisted configuration and
rendered requests, stops before client construction.

## Append-only execution and retries

The runner writes the exact rendered pair and attempt start before its first client call.
Each response or classified failure becomes a new outcome file; no outcome is updated in
place.

Transport and provider failures can be transient, so one arm may make at most three requests
with recorded request numbers. If an arm exhausts those requests, the attempt is an
infrastructure failure. The runner then starts one fresh pair attempt with a fresh attempt ID
and re-executes both arms. It never reuses a successful arm from the invalidated attempt.

```text
pair
├── attempt 1 ── complete ───────────────────────────────> terminal complete
│
└── attempt 1 ── infrastructure exhausted
    └── attempt 2 ── complete ───────────────────────────> terminal complete
                  └── infrastructure exhausted ──────────> infra_incomplete
```

This design trades resumability for a clearer audit boundary. A crash preserves the files
already written, but the CLI refuses to resume or overwrite that run root. An
`infra_incomplete` pair remains an honest terminal record and does not prevent the next of
the three intended pairs from running.

Before data can cross into blind export or reveal, the CLI verifies that the run contains
exactly the three manifest-declared terminal pair summaries. It re-derives pair IDs and order
seeds, reconciles each embedded attempt with its standalone start, summary, render, and every
append-only per-request outcome artifact, recomputes request and common-projection digests, and
checks them against the pre-call manifest. This prevents missing, extra, misidentified,
reordered, request-divergent, or summary-rewritten evidence from silently entering evaluation.

Refusal, provider-declared incompleteness, invalid structured output, and valid completion
are model-visible terminal outcomes, not infrastructure retries. Keeping those categories
separate prevents behavior from being silently relabeled as network noise.

## Mask, freeze, then reveal

Only pairs whose final fresh attempt has completed structured decisions for both arms are
eligible for blind review. `judge` derives fixed-length opaque IDs, sorts them, applies the
public seeded permutation, and publishes only:

- a manifest with ordered opaque IDs, output digests, and non-identifying exclusion counts;
- one opaque decision file per eligible output; and
- no pair ID, request digest, or official treatment assignment.

Judgments must cover every manifest ID exactly once, and each score must be a JSON integer
from 1 through 5; coercible strings, floats, and booleans are not accepted. Before freezing,
the committed public outputs are revalidated against their manifest digests, and the complete
typed `BlindManifest` is recomputed from the verified private run and public blind seed. Its
version, seeded opaque-ID order, output digests, and exclusion counts must match exactly.
`freeze_judgments` then canonicalizes the judgments into manifest order and commits both the
manifest digest and the judgment digest in an exclusive file. `reveal` repeats the source
manifest recomputation before it accepts the frozen gate, so coordinated edits to a manifest and
its frozen digests cannot authorize labels. It validates every public output, reconstructs the
official mapping from the private source run, and stages and validates the complete reveal
directory before making it visible.

The revealed report keeps three questions separate:

| Section | Question |
| --- | --- |
| Comparative ratings | How did the two arms compare on criteria both could satisfy? |
| Packet-uptake diagnostics | Did an output recognize and cite facts available specifically in the packet? |
| Arm-guess results | Could the evaluator guess the official arm, and with what confidence? |

Packet-only diagnostics are not folded into the comparative score. Doing so would reward one
arm for access the other arm was deliberately denied.

## Public seed and source-access trade-off

The blind seed is public for reproducibility. That choice also means label masking depends on
access control: anyone with both the seed and the labeled source run can recompute opaque IDs
and recover assignments before scoring. Give evaluators only the blind bundle until judgments
are frozen. Keep the run root private during evaluation.

Using a secret seed would reduce this operational risk but make independent reproduction
harder and turn seed custody into another hidden state. The harness chooses a public seed and
makes the source-access requirement explicit.

## What the result can and cannot support

The honest claim label is `one-case unscored information-availability pilot`. After blind
scores are revealed, the report is descriptive evidence for that same one case and execution,
not evidence of statistical significance.

The packet-off arm receives neither the packet nor the underlying 5,000 rows. It cannot run
an equivalent discovery process. A capable equal-discovery agent that can inspect the rows
could compute prevalence, missingness, likely identifier fields, baseline accuracy, and
metric implications itself. This pilot does not compare the packet with that agent and cannot
show that DSX beats it.

A broader causal claim requires multiple representative cases, pre-registered scoring, and
enough repetitions for uncertainty estimates. Data Access now supplies the explicit
full-data-discovery control, but an initial one-case execution remains descriptive.

## Data Access boundary

Data Access gives `full_data` full logical access to the prepared dataset through bounded,
read-only SQL and gives `dsx_packet` the opaque committed packet instead. It is a
capability-ceiling comparison: a discovery agent may inspect facts that are absent from the
packet, and it may spend more calls, time, tokens, and cost doing so. Those are measurements,
not hidden equalization rules.

The public blind bundle contains only decisions and claim statements. Packet payloads, SQL,
query results, evidence locators, source data, arm assignment, operational metrics, and
automatic claim scores remain private until judgments freeze. SQL result digests are replayed
against the frozen DuckDB database during reveal; generic packet pointers bind to the frozen
packet artifact but do not claim a computation replay without compatible packet tracing.

## Related documentation

- [CLI reference](cli-reference.md)
- [Context Lift](experiments/context-lift.md)
- [Data Access](experiments/data-access.md)
- [Project introduction](../README.md)
