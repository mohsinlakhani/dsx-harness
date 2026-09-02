# Repository architecture

## Vocabulary

Use **DSX Packet** in prose, `dsx-packet` for commands or artifact names, and `DsxPacket` in
Python. “Data context packet,” “profile packet,” and bare “packet” describe the same older
concept but should not be introduced in new public interfaces.

The two existing studies are experiments, not competing harnesses:

1. **Context Lift** is the original fixed packet-on/off pilot.
2. **Data Access** compares a DSX Packet with direct data discovery and their combination.

Names such as `pilot-v1`, `profile_packet`, and `packet.json` remain inside frozen Context Lift
artifacts where changing them would break reproducibility.

## Dependency direction

```text
                 dsx.packet
          stable product contracts
                    │
          consumed by applications
                    │
                    ▼
       future packet builders/routers

   dsx.experiments.context_lift    dsx.experiments.data_access
          frozen study                    active study
                    │                         │
                    └──── no cross-imports ───┘
```

Experiment packages may test a serialized DSX Packet, but reusable packet code must not
depend on experiment runners, scoring rubrics, provider clients, or historical fixtures.
Completed experiment schemas should remain readable even when the product packet evolves.

## DSX Packet boundary

The packet envelope owns only durable concerns:

- a schema version and packet identity;
- a dataset snapshot reference;
- uniquely identified, independently versioned modules;
- canonical serialization and a content commitment; and
- a task-scoped projection bound to its source packet.

Each `PacketModule.content` is arbitrary JSON. The module type identifies the owner of its
schema, while `schema_version` lets that owner evolve it independently. Suggested initial
namespaces are `profile.*`, `risk.*`, `decision.*`, `evidence.*`, and `question.*`.

Task specificity is a projection, not a second packet format. `DsTask` declares required
module types; `assemble_task_packet` selects those modules and records the source packet and
dataset digests. A later router can infer module types from a task while preserving this
contract and deterministic fallback.

## Where new work goes

| Change | Location |
| --- | --- |
| Packet envelope, modules, task projection | `src/dsx/packet/` |
| Dataset profiling or packet generation | a future `src/dsx/builders/` package |
| Task classification and module routing | a future `src/dsx/routing/` package |
| Experiment-specific arms, ledgers, scoring | `src/dsx/experiments/<experiment>/` |
| Generated inputs, runs, and blind bundles | ignored `artifacts/<experiment>/` |
| Stable protocol and result narrative | `docs/experiments/` |

Avoid adding shared code merely because two frozen experiments contain similar functions.
Extract a component only when the current product needs a stable abstraction and its contract
can be named independently of either study.

## Next increments

1. Add typed validators and builders for the first earned module types while retaining the
   arbitrary JSON envelope.
2. Generate DSX Packets from CSV and Parquet with dataset fingerprints and evidence refs.
3. Define a small data-science task taxonomy and a deterministic task-to-module policy.
4. Compare full versus task-scoped packets through configuration in a new experiment.
5. Add post-decision checks that resolve claims against the exact task packet used.
