# Data Access experiment results

- **Run date:** 2026-08-27
- **Model:** `gpt-5.6-terra`
- **Case:** `pilot-20260819` — 5,000 rows, 7 columns, 2% positive class
- **Design:** Three fresh repetitions of all three arms
**Review:** Nine eligible outputs, independently judged under opaque IDs before reveal

## Executive summary

The `packet_and_full_data` arm delivered the strongest overall balance. It tied `full_data`
for blind decision and evidence quality, led on limitations quality, and used materially less
time, cost, and discovery effort than full data alone.

| Metric | `dsx_packet` | `full_data` | `packet_and_full_data` |
| --- | ---: | ---: | ---: |
| Blind decision quality (1–5) | 4.00 | 5.00 | 5.00 |
| Blind evidence use (1–5) | 3.33 | 5.00 | 5.00 |
| Blind limitations quality (1–5) | 3.67 | 4.00 | 5.00 |
| Mean elapsed time | 11.30 s | 29.50 s | 20.81 s |
| Mean inference cost | $0.01444 | $0.03409 | $0.02483 |
| Mean model calls | 1.00 | 10.00 | 3.67 |
| Mean SQL calls | 0.00 | 9.00 | 2.67 |
| Total tokens across three runs | 7,976 | 86,095 | 30,581 |

Relative to `full_data`, the combined arm was 29.5% faster, 27.2% cheaper, and used 63.3%
fewer model calls. Relative to `dsx_packet`, it was 84.1% slower and 72.0% more expensive.

**Conclusion:** on this single synthetic case, the packet compressed much of the value of broad
discovery, while a small amount of targeted SQL added the strongest qualitative safeguards.
This is descriptive evidence only, not a general superiority claim.

## Experiment design

[Data Access v2](data-access.md) compares three fresh executions of the same committed model,
task, response schema, output-token limit, and terminal decision contract:

- `dsx_packet` received an opaque committed packet and no dataset tool.
- `full_data` received read-only SQL access to every dataset row and no packet.
- `packet_and_full_data` received both sources.

Arm order was randomized within each repetition using seed `20260827`. All three repetitions
and all nine arm executions completed. No output was excluded from blind review. There were no
provider errors, transport errors, abandoned runs, or infrastructure retries. Two SQL attempts
were policy-rejected—one in `full_data` and one in `packet_and_full_data`—without preventing a
terminal decision.

The independent judge received only the public blind bundle: opaque decisions and claim
statements. Arm identity, packet contents, SQL, evidence locators, usage, costs, repetition
identity, and automatic scores were hidden until all judgments were frozen. The judge scored
decision quality, evidence use, and limitations quality from 1 to 5, then guessed the arm.

## Results

### Blind qualitative review

| Arm | Decisions | Decision quality | Evidence use | Limitations quality | Arm-guess accuracy | Mean confidence |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `dsx_packet` | 3 | 4.00 | 3.33 | 3.67 | 100% | 5.00 |
| `full_data` | 3 | 5.00 | 5.00 | 4.00 | 100% | 5.00 |
| `packet_and_full_data` | 3 | 5.00 | 5.00 | 5.00 | 100% | 4.67 |

The evaluator identified all nine treatments correctly. The review was label-blind, but the
responses were fully distinguishable by behavior and evidence style.

### Operational results

| Arm | Completed | Total elapsed | Model calls | SQL calls | Failed SQL | Total tokens | Total inference cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `dsx_packet` | 3/3 | 33.91 s | 3 | 0 | 0 | 7,976 | $0.04331 |
| `full_data` | 3/3 | 88.51 s | 30 | 27 | 1 | 86,095 | $0.10226 |
| `packet_and_full_data` | 3/3 | 62.43 s | 11 | 8 | 1 | 30,581 | $0.07449 |

Across all arms, the run used 124,652 tokens, accumulated 184.86 seconds of arm elapsed time,
and had an estimated inference cost of $0.22006. These costs use the committed pricing snapshot.
No packet-build metrics were supplied, so the comparison excludes packet construction and
amortization.

### Claim and evidence outcomes

| Arm | Supported | Unsupported | Contradicted | Unverifiable | Evidence resolution | SQL replay |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `dsx_packet` | 3 | 1 | 0 | 7 | 63.6% (7/11) | N/A |
| `full_data` | 10 | 0 | 6 | 3 | 100% (19/19) | 100% (19/19) |
| `packet_and_full_data` | 1 | 0 | 0 | 15 | 100% (19/19) | 100% (4/4) |

Automatic claim classifications are protocol-sensitive. A resolved or replayable reference is
not automatically classified as supporting evidence unless the claim uses the registered
predicate and exact proof shape required by Evidence Protocol v1.

## Analysis

### The combined arm produced the best quality–efficiency balance

`packet_and_full_data` achieved the maximum blind score in all three qualitative dimensions,
yet averaged only 3.67 model calls and 2.67 SQL calls per decision, compared with 10 and 9 for
`full_data`. The packet appears to have supplied enough global structure to narrow discovery to
a few high-value checks. The full-data arm had to reconstruct that structure through repeated
queries.

### Packet-only delivery was efficient but less reliable

`dsx_packet` was the fastest and cheapest arm and still produced good decisions, but evidence
use averaged 3.33/5. One repetition generated four non-resolving JSON pointers, reducing the
arm's aggregate evidence-resolution rate to 63.6%. The other two repetitions resolved every
pointer. This variance suggests that packet content was useful, but pointer generation is not
yet dependable enough for evidence-critical use without validation or constrained selection.

### Strict claim scores partly measured protocol alignment

`full_data` produced the most automatically supported claims: 10 of 19. Its six
`contradicted` classifications were not obvious factual reversals. They arose mainly from
asserted-value shape mismatches:

- a missing-row count of `250` where the oracle expects a missingness rate of `0.05`;
- a distinct count or ratio where `likely_id` expects a boolean; and
- a scalar `row_id` where `recommended_exclusions` expects a list.

Three class-count statements were also unverifiable because they combined both labels into an
unregistered proof shape.

The combined arm's automatic support rate was only 1 of 16 claims despite perfect blind
evidence scores and 100% evidence resolution. Fifteen claims used semantically sensible but
unregistered predicates such as `dataset_shape`, `class_distribution`, label-conditional
feature ranges, or stated limitations. The oracle classified them as unverifiable. The gap
between human judgment and automatic classification exposes a vocabulary and schema-alignment
limitation in Evidence Protocol v1, not a simple failure of factual reasoning.

### Treatment leakage limited the blindness claim

The judge guessed every arm correctly. Sparse packet citations, extensive SQL-derived detail,
and mixed evidence styles made the treatments recognizable. The qualitative scores should be
interpreted as independent review without arm labels, not as proof that presentation cues were
neutralized.

## Conclusion

For this committed case, `packet_and_full_data` is the preferred operating point when
qualitative robustness matters. It matched the full-data arm's decision and evidence scores,
exceeded its limitations score, and required substantially less discovery. `dsx_packet` is the
preferred arm when latency and inference cost dominate, provided packet pointers are validated.

The experiment reveals a clear trade-off. Full-data discovery maximized qualitative decision
and evidence quality but consumed the most time, tokens, and tool calls. Packet delivery reduced
mean inference cost by 57.7% and mean elapsed time by 61.7% relative to full data while retaining
good decision quality. Adding targeted full-data access to the packet restored top qualitative
scores at 27.2% lower cost than unrestricted discovery alone.

This experiment does not establish that DSX is generally better. It is one deterministic
synthetic case with three repetitions, no inferential statistics, no packet-build cost, and
visible treatment signatures. It supports the narrower conclusion that, under this case and
budget, packet delivery reduced discovery work substantially and the combined condition
delivered the best observed quality–efficiency compromise.

## Recommended follow-up

### Align the evidence protocol

Expose canonical asserted-value types and proof shapes for `likely_id`, `missingness`,
exclusions, class counts, and composite distribution claims. Add schema-constrained examples so
factual correctness is not obscured by avoidable shape mismatches.

### Harden packet references

Validate generated JSON pointers before terminal submission or offer an enumerated pointer
vocabulary. The single all-invalid packet repetition materially reduced evidence reliability
despite otherwise sound decisions.

### Measure packet lifecycle cost

Capture packet-build time, tokens, and cost so amortized comparisons at realistic reuse counts
can be reported. Current cost conclusions cover inference only.

### Expand the case set

Repeat the pre-specified three-arm protocol across representative datasets, model families, and
data conditions before making broader capability or economic claims. Consider a
presentation-normalized review layer if behavior-blind comparison is an explicit goal.

## Run record

- Run root: `data-access-run-v2-20260827-all-arms`
- Prepared inputs: `data-access-inputs-v2-20260827`
- Blind bundle: `data-access-blind-v2-20260827`
- Order seed: `20260827`
- Blind seed: `8272026`
- Frozen judgment digest:
  `a90ea36da7aa218552d3aa05ee013a1c1a4c84e7911df1bcada5c998cd8e398b`
- Revealed report: `data-access-blind-v2-20260827/reveal/revealed_report.json`

The private run root contains arm identities, prompts, SQL, usage, and raw provider envelopes and
should remain controlled.
