"""Independent, test-only observations of the frozen pilot fixture."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from dsx.pilot.models import Packet, PilotCase


def canonical_case_digest(case: PilotCase) -> str:
    """Hash the case JSON independently of the production digest helper."""
    payload = json.dumps(
        case.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def class_counts(case: PilotCase) -> Counter[int]:
    """Count labels by inspecting fixture rows."""
    return Counter(row.label for row in case.rows)


def missing_indices(case: PilotCase) -> set[int]:
    """Return row positions where the nullable field is absent."""
    return {index for index, row in enumerate(case.rows) if row.nullable_numeric is None}


def numeric_packet_facts(packet: Packet) -> dict[str, float | int]:
    """Expose packet numeric claims for comparison with raw-row observations."""
    facts: dict[str, float | int] = {
        "rows": packet.dataset_shape.rows,
        "columns": packet.dataset_shape.columns,
        "majority_baseline_accuracy": packet.majority_baseline_accuracy,
    }
    facts.update({f"count_{fact.label}": fact.count for fact in packet.class_counts})
    facts.update({f"rate_{fact.label}": fact.rate for fact in packet.class_rates})
    facts.update(
        {f"missingness_{fact.name}": fact.missingness for fact in packet.column_facts}
    )
    return facts
