# DSX experiment harness

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](https://www.python.org/)

DSX contains two deliberately separate experiments. Both keep prompts, execution, and
evaluation evidence inspectable rather than treating them as operator assumptions.

**Context Lift** is the frozen packet-on versus packet-off information-availability pilot. Its
claim remains deliberately narrow: **one-case unscored information-availability pilot**.

**Data Access v2** compares three fresh same-model arms: opaque `dsx_packet`, full data via a
bounded read-only SQL tool, and `packet_and_full_data`. It tests a capability ceiling and
measures quality, time, calls, token/cost use, failed discovery, and evidence reproducibility.
Initial one-case results are descriptive; they do not establish statistical significance or
general model superiority.

## Context Lift: visible offline result

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

1. Install all dependency groups.

   ```bash
   uv sync --all-groups
   ```

2. Generate the deterministic case and proved request pair. Generation does not call a
   provider, so `offline-example` is only a recorded identifier.

   ```bash
   uv run dsx-pilot generate pilot-generated --model offline-example
   ```

3. Inspect the four human-readable typed artifacts.

   ```bash
   ls pilot-generated
   ```

   You should see `case.json`, `packet.json`, `rendered_requests.json`, and
   `request_configuration.json`. The generate command also prints the case, request, and
   common-projection digests without dumping 5,000 rows.

Every destination is exclusive. If `pilot-generated` already exists, choose a new path; the
harness never overwrites or resumes published evidence.

## Context Lift: full workflow

Set your exact model identifier and credential once in `.env` (copy `.env.example` and replace
the placeholders), then generate a new live input directory:

```bash
uv run dsx-pilot generate pilot-live-inputs
uv run dsx-pilot run pilot-live-inputs pilot-run --order-seed 731
```

`--model` remains available when you need a one-off override. `.env` is ignored by Git, while
`.env.example` is the safe template to share.

`run` makes paid live OpenAI requests and executes exactly three pairs sequentially. It
re-loads and re-proves the generated requests immediately before the client boundary, writes
the run manifest before pair calls, and gives every intended pair a terminal summary.

Export decisions for an evaluator, freeze complete blind judgments, then reveal:

```bash
uv run dsx-pilot judge pilot-run pilot-blind --blind-seed 991
uv run dsx-pilot judge pilot-run pilot-blind --blind-seed 991 \
  --judgments judgments.json
uv run dsx-pilot reveal pilot-run pilot-blind
```

Do not give evaluators the private `pilot-run` directory. Because the blind seed is public,
the seed plus labeled source artifacts can reconstruct treatment labels before scoring.

## Data Access

Prepare an arbitrary packet and a JSON case configuration, then run the v2 three-arm experiment:

```bash
uv run dsx-data-access prepare case.json dsx-packet.json data-access-inputs \
  --model "$MODEL_ID" --pricing pricing.json
uv run dsx-data-access run data-access-inputs data-access-run --order-seed 731
uv run dsx-data-access judge data-access-run data-access-blind --blind-seed 991
uv run dsx-data-access judge data-access-run data-access-blind --blind-seed 991 \
  --judgments judgments.json
uv run dsx-data-access reveal data-access-run data-access-blind
```

Only `run` needs `OPENAI_API_KEY` and may incur provider cost. The full-data and combined arms
receive read-only SQL access to the prepared table named `dataset`; the packet and combined arms
receive the same committed packet. Packet contents, SQL text/results, evidence locators, usage,
and automatic scores never enter the public blind bundle. See [Data Access](docs/data-access.md)
for the case and pricing schemas and its claim boundary.

## Documentation

- [CLI reference](docs/cli-reference.md): every command, option, artifact, and error boundary.
- [How to run the pilot](docs/how-to-run-pilot.md): prerequisites through blind reveal and troubleshooting.
- [Evidence boundary](docs/evidence-boundary.md): sole-delta proof, retries, masking, trade-offs, and claim limits.
- [Data Access](docs/data-access.md): full-data discovery protocol, SQL safety boundary, and artifacts.

## Development gate

The required offline gate enforces 100% statement and branch coverage for the experiment
packages and does not require provider credentials:

```bash
uv run pytest -m "not live" --cov=dsx.pilot --cov=dsx.experiments.data_access --cov-branch --cov-fail-under=100
```

Run the remaining checks and package build with:

```bash
uv run ruff check .
uv run mypy src
uv build
```

## License

Licensed under the [Apache License 2.0](LICENSE).
