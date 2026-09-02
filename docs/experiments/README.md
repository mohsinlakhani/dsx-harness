# Experiments

DSX harness currently preserves two completed studies:

| Experiment | Protocol | Supporting material |
| --- | --- | --- |
| Context Lift | [Runbook](context-lift.md) | [Evidence boundary](../evidence-boundary.md) |
| Data Access | [Protocol](data-access.md) | [Design](data-access-design.md), [results](data-access-results.md) |

Context Lift tests the effect of adding a DSX Packet to an otherwise controlled request. Data
Access compares packaged context with direct dataset discovery and the combination of both.
New studies should receive their own package under `src/dsx/experiments/`, matching tests, and
a protocol here; they should consume rather than define reusable DSX Packet behavior.
