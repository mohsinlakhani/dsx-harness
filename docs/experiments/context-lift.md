# Context Lift experiment

This guide takes one generated case through three live pairs, blind export, immutable
judgment freeze, and reveal. The result is a descriptive one-case report with a durable
request and evaluation ledger.

## Prerequisites

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/) installed.
- A valid OpenAI API key and a model identifier available to that account.
- A separate evaluator who receives only the blind bundle, not the private run root.

The `run` step makes live provider calls that cost money. `generate`, blind export, judgment
freeze, reveal, and the test gate are local operations.

## 1. Install the project

From the repository root, install runtime and development groups:

```bash
uv sync --all-groups
```

Confirm the four commands are available:

```bash
uv run dsx-context-lift --help
```

## 2. Generate and inspect the input bundle

Generation is offline. The model identifier is recorded but not called.

```bash
uv run dsx-context-lift generate pilot-generated --model offline-example
```

The command prints four digests without printing the 5,000 rows. Confirm the four typed
artifacts exist:

```bash
ls pilot-generated
```

Expected names:

```text
case.json
packet.json
rendered_requests.json
request_configuration.json
```

For the default seed, the printed case digest must be
`57eec293b8b511ac9c1cf244eded3dddd483f375b47435bd92db67dcc5af5c9a`.

Before a live run, copy `.env.example` to `.env` and set the exact model identifier and API
key you intend to call. The local `.env` file is ignored by Git. Then regenerate with a new
directory because generation never overwrites:

```bash
uv run dsx-context-lift generate pilot-live-inputs
```

## 3. Run three live pairs

The credential is loaded from `.env`. Do not put it in a command argument, JSON artifact,
documentation, or shell history.

Choose and record a base order seed, then start a new run root:

```bash
uv run dsx-context-lift run pilot-live-inputs pilot-run --order-seed 731
```

The command validates and re-renders the generated inputs before constructing the provider
client. It writes `pilot-run/run_manifest.json` before pair execution and prints one terminal
line for each of the three intended pairs. A terminal `infra_incomplete` pair does not stop
the remaining pairs.

Keep `pilot-run` private until evaluation is frozen. It contains official arm labels and the
data needed to reconstruct the public opaque IDs.

## 4. Export the blind bundle

Choose a public blind seed and export to another new path:

```bash
uv run dsx-context-lift judge pilot-run pilot-blind --blind-seed 991
```

The command prints the ordered opaque IDs and the exact judgment object shape. Give the
evaluator `pilot-blind`, the ordered IDs, and the scoring rubric. Do not give the evaluator
`pilot-run`. A public seed plus labeled source artifacts can reconstruct official labels.

Only pairs with completed structured decisions for both arms are exported. Inspect
`pilot-blind/manifest.json` for `eligible_count` and non-identifying exclusion counts.

## 5. Create the judgment file

Create `judgments.json` as a JSON array with one object for every printed opaque ID. Replace
the example ID and scores; every score must be an integer from 1 through 5.

```json
[
  {
    "opaque_id": "replace-with-first-opaque-id",
    "decision_quality": 4,
    "evidence_use": 3,
    "limitations_quality": 4,
    "packet_guess": "packet_on",
    "guess_confidence": 2,
    "metric_reasoning": 4,
    "split_strategy": 4,
    "leakage_row_id_avoidance": 5,
    "limitations": 3,
    "overall_recommendation_quality": 4,
    "exact_prevalence_recognition": 3,
    "majority_baseline_recognition": 3,
    "citation_use": 2
  }
]
```

`packet_guess` is the evaluator's guess, not an official assignment. Valid values are
`packet_off` and `packet_on`. Add one complete object per remaining opaque ID. Do not include
duplicates or IDs absent from the manifest. Use JSON integer literals for every score; quoted
numbers, decimal values such as `5.0`, and booleans are rejected rather than converted.

## 6. Freeze judgments

Freeze the complete array against the existing public manifest and the same blind seed:

```bash
uv run dsx-context-lift judge pilot-run pilot-blind --blind-seed 991 \
  --judgments judgments.json
```

The command prints `pilot-blind/frozen_judgments.json` and its canonical digest. Review and
archive that file before reveal. Before writing it, the command rechecks every public decision
against the digest committed at export. A changed blind output and a second freeze are refused.

## 7. Reveal labels and report

After the freeze exists, join the opaque records to their official source assignments:

```bash
uv run dsx-context-lift reveal pilot-run pilot-blind
```

The command publishes two files under `pilot-blind/reveal/` and prints three separate report
sections:

- comparative ratings available to both arms;
- packet-uptake diagnostics kept outside the fair comparison; and
- arm-guess accuracy and confidence.

Reveal is exclusive. A second invocation is refused instead of replacing the report.

## 8. Verify the repository gate

Run the exact offline test and branch-coverage gate:

```bash
uv run pytest -m "not live" --cov=dsx.experiments.context_lift --cov-branch --cov-fail-under=100
```

Then run the static checks and package build:

```bash
uv run ruff check .
uv run mypy src
uv build
```

None of these commands need provider credentials or make live requests.

## Troubleshooting

### A destination already exists

Generated directories, run roots, blind bundles, frozen judgments, and reveal directories
are immutable publication boundaries. Inspect the existing evidence and choose a new path;
do not delete or reuse it merely to make a command pass.

### `OPENAI_API_KEY is required`

The key is absent or empty in the process environment. Export it in the same shell that runs
`dsx-context-lift run`. Do not pass the value on the command line.

### Generated artifact or digest mismatch

One of `case.json`, `packet.json`, `request_configuration.json`, or
`rendered_requests.json` is missing, malformed, stale, or changed. Preserve that directory for
investigation and run `generate` into a new directory with the intended options.

### A pair is `infra_incomplete`

Both fresh attempts exhausted recorded transport/provider retries. The CLI continues the
other intended pairs. Keep the terminal summary; blind export records an exclusion rather
than treating it as a scored comparison.

### Judgment JSON is rejected

Check that the top-level value is an array; every manifest ID appears exactly once; there are
no extra IDs; all 12 rating/confidence fields are integers from 1 through 5; and
`packet_guess` is `packet_off` or `packet_on`. The [CLI reference](../cli-reference.md) lists every
field.

### Reveal is refused before freeze

Run `judge` with `--judgments` successfully first. If a frozen file exists but reveal still
fails, a manifest, public output, source decision, or judgment commitment has changed. Do not
bypass the gate; compare the committed digests and restart with preserved source evidence if
necessary.

## Related documentation

- [CLI reference](../cli-reference.md)
- [Evidence boundary](../evidence-boundary.md)
- [Project introduction](../../README.md)
