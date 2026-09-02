# DSX harness

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB.svg)](https://www.python.org/)

DSX harness develops and evaluates **DSX Packets**: inspectable, evidence-backed context that
helps an agent make a data-science decision. The repository separates the reusable packet
domain from completed experiments so new product work does not accrete inside a pilot runner.

## Repository map

```text
src/dsx/
├── packet/                         # reusable DSX Packet contracts and task projections
├── pipeline/                       # transformation-manifest contracts and graph validation
├── builders/                       # history-aware packet builder and `dsx-packet` CLI
└── experiments/
    ├── context_lift/               # experiment 1: packet versus no packet
    └── data_access/                # experiment 2: packet, data discovery, or both

tests/
├── packet/
├── pipeline/
├── builders/
└── experiments/                    # mirrors src/dsx/experiments

docs/
├── architecture.md                 # boundaries and naming
├── roadmap.md                      # product and experiment direction
└── experiments/                    # protocols for completed experiments
```

Generated inputs, run ledgers, and blind bundles belong under `artifacts/`, which is ignored
by Git. Historical root-level output paths continue to work and are also ignored.

## DSX Packet

`DsxPacket` is a stable envelope containing independently versioned modules. A module has a
namespaced type and arbitrary JSON content, so population profiles, feature risks, operating
constraints, provenance, and future packet capabilities can evolve without expanding one
monolithic model.

```python
from dsx.packet import DatasetRef, DsTask, DsxPacket, PacketModule, assemble_task_packet

packet = DsxPacket(
    packet_id="credit-v1",
    dataset=DatasetRef(digest="a" * 64),
    modules=(
        PacketModule(
            module_id="population",
            module_type="profile.population",
            schema_version="1",
            content={"rows": 5_000},
        ),
        PacketModule(
            module_id="feature-risks",
            module_type="risk.features",
            schema_version="1",
            content={"likely_ids": ["row_id"]},
        ),
    ),
)

task_packet = assemble_task_packet(
    packet,
    DsTask(
        task_id="feature-review-1",
        task_type="feature-review",
        objective="Review candidate features for leakage.",
        module_types=("profile.population", "risk.features"),
    ),
)
```

Task assembly is deliberately declarative for now: the task says which module types it
requires, and assembly fails if the source packet cannot satisfy it. A learned or rule-based
router can later produce the same `DsTask` contract without changing packet storage.

Build a packet from a CSV or Parquet file, optionally with a declared transformation
manifest:

```bash
uv run dsx-packet build data/train.parquet artifacts/packet-bundle \
  --target label \
  --packet-id fraud-v1 \
  --manifest data/transforms.json
```

The command writes an exclusive bundle containing `packet.json`, `build-record.json`, and a
copy of the normalized manifest when one was supplied. It does not copy the dataset.

## Experiments

The repository contains two completed, deliberately bounded studies:

| Experiment | Question | Arms | CLI |
| --- | --- | --- | --- |
| **Context Lift** | Does supplied dataset context change the decision? | packet off, packet on | `dsx-context-lift` |
| **Data Access** | How does packaged context compare with on-demand discovery? | DSX Packet, full data, both | `dsx-data-access` |

Context Lift is the frozen original pilot. Its historical contracts and artifact field names
remain intact for reproducibility. `dsx-pilot` remains as a compatibility alias for its CLI.
Data Access accepts arbitrary packet JSON, including the new `DsxPacket` envelope, and keeps
that content opaque during execution.

Generate a visible offline Context Lift fixture:

```bash
uv sync --all-groups
uv run dsx-context-lift generate artifacts/context-lift/generated --model offline-example
```

Run Data Access after preparing a case, packet, and pricing snapshot:

```bash
uv run dsx-data-access prepare case.json dsx-packet.json artifacts/data-access/inputs \
  --model "$MODEL_ID" --pricing pricing.json
uv run dsx-data-access run artifacts/data-access/inputs artifacts/data-access/run \
  --order-seed 731
```

Only experiment `run` commands require `OPENAI_API_KEY` and may incur provider cost. Both
experiments use exclusive destinations and will not overwrite published evidence.

## Development

The offline gate does not require provider credentials:

```bash
uv run pytest -m "not live" \
  --cov=dsx.packet \
  --cov=dsx.pipeline \
  --cov=dsx.builders \
  --cov=dsx.experiments.context_lift \
  --cov=dsx.experiments.data_access \
  --cov-branch --cov-fail-under=100
uv run ruff check .
uv run mypy src
uv build
```

See [architecture](docs/architecture.md), [CLI reference](docs/cli-reference.md), and the
[roadmap](docs/roadmap.md) for the next development steps. Experiment protocols live under
[docs/experiments](docs/experiments/README.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
