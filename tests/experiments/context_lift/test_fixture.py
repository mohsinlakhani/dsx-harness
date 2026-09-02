"""Behavioral oracle tests for the deterministic Context Lift fixture."""

from __future__ import annotations

import random
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from dsx.experiments.context_lift.models import (
    FROZEN_CASE_DIGEST,
    candidate_packet,
    generate_pilot_case,
)
from tests.experiments.context_lift.fixtures.oracle import (
    canonical_case_digest,
    class_counts,
    missing_indices,
    numeric_packet_facts,
)


def test_default_case_matches_the_frozen_digest_snapshot() -> None:
    """Changing any serialized default-case value must invalidate its snapshot."""
    assert canonical_case_digest(generate_pilot_case()) == FROZEN_CASE_DIGEST


def test_default_case_has_the_controlled_schema_distribution_and_missingness() -> None:
    """Fixture drift must not alter its fixed size, labels, IDs, or null pattern."""
    case = generate_pilot_case()

    assert len(case.rows) == 5_000
    assert class_counts(case) == {0: 4_900, 1: 100}
    assert {index for index, row in enumerate(case.rows) if row.label == 1} == set(
        random.Random(20260819).sample(range(5_000), 100)
    )
    assert {row.label for row in case.rows} == {0, 1}
    assert all(
        re.fullmatch(r"case-\d{5}", row.row_id) is not None for row in case.rows
    )
    assert [row.row_id for row in case.rows] == [f"case-{index:05d}" for index in range(5_000)]
    assert len({row.row_id for row in case.rows}) == 5_000
    assert set(case.rows[0].model_dump()) == {
        "row_id",
        "label",
        "signal_a",
        "signal_b",
        "noise",
        "category",
        "nullable_numeric",
    }
    assert missing_indices(case) == set(range(0, 5_000, 20))
    assert all(
        row.nullable_numeric is not None
        for index, row in enumerate(case.rows)
        if index % 20 != 0
    )


def test_default_case_uses_the_hand_derived_signal_noise_and_category_formulas() -> None:
    """Changing a formula must not silently weaken label signal or independent noise."""
    case = generate_pilot_case()

    assert {row.category for row in case.rows} == {"north", "south", "east", "west"}
    categories = ("north", "south", "east", "west")
    for index, row in enumerate(case.rows):
        assert row.category == categories[index % 4]
        assert row.signal_a == float(row.label * 1_000 + (index * 17) % 100)
        assert row.signal_b == float(row.label * 2_000 + (index * 29) % 100)
        assert row.noise == float((index * 37 + 13) % 100)
        expected_nullable = None if index % 20 == 0 else float((index * 43) % 100)
        assert row.nullable_numeric == expected_nullable


def test_hand_authored_packet_numeric_facts_match_raw_fixture_observations() -> None:
    """Changing a packet number must be caught against independently observed rows."""
    case = generate_pilot_case()
    facts = numeric_packet_facts(candidate_packet())
    observed_counts = class_counts(case)

    assert facts["rows"] == len(case.rows)
    assert facts["columns"] == len(case.rows[0].model_dump())
    assert facts["count_0"] == observed_counts[0]
    assert facts["count_1"] == observed_counts[1]
    assert facts["rate_0"] == observed_counts[0] / len(case.rows)
    assert facts["rate_1"] == observed_counts[1] / len(case.rows)
    assert facts["majority_baseline_accuracy"] == max(observed_counts.values()) / len(case.rows)
    for name in case.rows[0].model_dump():
        observed_missingness = sum(
            getattr(row, name) is None for row in case.rows
        ) / len(case.rows)
        assert facts[f"missingness_{name}"] == observed_missingness


def test_packet_guidance_and_evidence_cover_the_observed_fixture_facts() -> None:
    """Packet changes must retain the required recommendations and supported evidence."""
    packet = candidate_packet()
    evidence = " ".join(packet.evidence_references).lower()

    assert packet.version == "pilot-v1"
    assert [fact.name for fact in packet.column_facts] == [
        "row_id",
        "label",
        "signal_a",
        "signal_b",
        "noise",
        "category",
        "nullable_numeric",
    ]
    assert [fact.name for fact in packet.column_facts if fact.likely_id] == ["row_id"]
    assert "recall" in packet.metric_guidance.lower()
    assert "5%" in packet.metric_guidance
    assert "precision" in packet.metric_guidance.lower()
    assert "pr-auc" in packet.metric_guidance.lower()
    assert "accuracy" in packet.metric_guidance.lower()
    assert "stratif" in packet.split_guidance.lower()
    assert "5%" in packet.split_guidance
    assert packet.exclusions == ("row_id",)
    assert any("synthetic" in limitation.lower() for limitation in packet.limitations)
    assert any("unscored" in limitation.lower() for limitation in packet.limitations)
    assert any("packet-off" in limitation.lower() for limitation in packet.limitations)
    for required_fact in (
        "row count",
        "class distribution",
        "row-id uniqueness",
        "nullable missingness",
    ):
        assert required_fact in evidence


@settings(max_examples=8, deadline=None)
@given(seed=st.integers())
def test_case_invariants_hold_across_supported_seeds(seed: int) -> None:
    """Changing seed handling must not change controlled case-level invariants."""
    case = generate_pilot_case(seed)

    assert len(case.rows) == 5_000
    assert class_counts(case) == {0: 4_900, 1: 100}
    assert len({row.row_id for row in case.rows}) == 5_000
    assert missing_indices(case) == set(range(0, 5_000, 20))
