# Experiments

DSX harness currently preserves two completed studies and one Data Access follow-on
protocol. The Luna realistic slice reuses the Data Access v2 harness; it is not a third
experiment package.

| Experiment | Protocol | Supporting material |
| --- | --- | --- |
| Context Lift | [Runbook](context-lift.md) | [Evidence boundary](../evidence-boundary.md) |
| Data Access | [Protocol](data-access.md) | [Design](data-access-design.md), [results](data-access-results.md) |
| Data Access (Luna realistic slice) | [Protocol](data-access-luna-realistic.md) | Follow-on study: generated packets on frozen DataSciBench tables; Terra results are not pooled |

Context Lift tests the effect of adding a DSX Packet to an otherwise controlled request. Data
Access compares packaged context with direct dataset discovery and the combination of both.
New studies should receive their own package under `src/dsx/experiments/`, matching tests, and
a protocol here; they should consume rather than define reusable DSX Packet behavior. Follow-on
Data Access studies that keep the v2 runner may add freeze/suite/uptake helpers and a protocol
here instead of a new package.
