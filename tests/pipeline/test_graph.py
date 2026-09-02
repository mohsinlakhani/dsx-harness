"""Tests for in-memory transformation graph validation and traversal."""

from __future__ import annotations

import pytest

from dsx.pipeline import SnapshotRole, TransformationGraph, TransformationOperation
from tests.builders.helpers import manifest, snapshot, step


def _linear() -> TransformationGraph:
    return TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("natural", "b" * 64, SnapshotRole.intermediate),
                snapshot("train", "c" * 64, SnapshotRole.train),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("natural",)),
                step("augment", TransformationOperation.augment, ("natural",), ("train",)),
            ),
        )
    )


def test_round_trip_branching_joining_and_multi_output_manifests() -> None:
    branching = TransformationGraph.from_manifest(
        manifest(
            current="left",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("left", "b" * 64, SnapshotRole.train),
                snapshot("right", "c" * 64, SnapshotRole.test),
            ),
            steps=(
                step("branch", TransformationOperation.split, ("raw",), ("left", "right")),
            ),
        )
    )
    joining = TransformationGraph.from_manifest(
        manifest(
            current="joined",
            snapshots=(
                snapshot("a", "a" * 64, SnapshotRole.source),
                snapshot("b", "b" * 64, SnapshotRole.source),
                snapshot("joined", "c" * 64, SnapshotRole.train),
            ),
            steps=(step("join", TransformationOperation.join, ("a", "b"), ("joined",)),),
        )
    )
    assert branching.step_outputs("branch") == ("left", "right")
    assert joining.snapshot_ancestors("joined") == ("a", "b")


def test_rejects_duplicate_ids_and_missing_references() -> None:
    with pytest.raises(ValueError, match="duplicate snapshot IDs"):
        TransformationGraph.from_manifest(
            manifest(
                current="raw",
                snapshots=(
                    snapshot("raw", "a" * 64, SnapshotRole.source),
                    snapshot("raw", "b" * 64, SnapshotRole.source),
                ),
            )
        )
    with pytest.raises(ValueError, match="duplicate step IDs"):
        TransformationGraph.from_manifest(
            manifest(
                current="train",
                snapshots=(
                    snapshot("raw", "a" * 64, SnapshotRole.source),
                    snapshot("mid", "b" * 64, SnapshotRole.intermediate),
                    snapshot("train", "c" * 64, SnapshotRole.train),
                ),
                steps=(
                    step("same", TransformationOperation.filter, ("raw",), ("mid",)),
                    step("same", TransformationOperation.augment, ("mid",), ("train",)),
                ),
            )
        )
    with pytest.raises(ValueError, match="missing snapshot"):
        TransformationGraph.from_manifest(
            manifest(
                current="raw",
                snapshots=(snapshot("raw", "a" * 64, SnapshotRole.source),),
                steps=(step("bad", TransformationOperation.filter, ("raw",), ("gone",)),),
            )
        )


def test_rejects_producer_and_source_invariants() -> None:
    with pytest.raises(ValueError, match="more than one producer"):
        TransformationGraph.from_manifest(
            manifest(
                current="train",
                snapshots=(
                    snapshot("raw", "a" * 64, SnapshotRole.source),
                    snapshot("other", "b" * 64, SnapshotRole.source),
                    snapshot("train", "c" * 64, SnapshotRole.train),
                ),
                steps=(
                    step("one", TransformationOperation.filter, ("raw",), ("train",)),
                    step("two", TransformationOperation.filter, ("other",), ("train",)),
                ),
            )
        )
    with pytest.raises(ValueError, match="source snapshot raw has a producer"):
        TransformationGraph.from_manifest(
            manifest(
                current="raw",
                snapshots=(
                    snapshot("other", "a" * 64, SnapshotRole.source),
                    snapshot("raw", "b" * 64, SnapshotRole.source),
                ),
                steps=(step("make", TransformationOperation.filter, ("other",), ("raw",)),),
            )
        )
    with pytest.raises(ValueError, match="non-source snapshot train has no producer"):
        TransformationGraph.from_manifest(
            manifest(
                current="raw",
                snapshots=(
                    snapshot("raw", "a" * 64, SnapshotRole.source),
                    snapshot("train", "b" * 64, SnapshotRole.train),
                ),
            )
        )


def test_rejects_missing_unreachable_and_cyclic_current() -> None:
    with pytest.raises(ValueError, match="current snapshot is missing"):
        TransformationGraph.from_manifest(
            manifest(
                current="missing",
                snapshots=(snapshot("raw", "a" * 64, SnapshotRole.source),),
            )
        )
    with pytest.raises(ValueError, match="not reachable from a source"):
        TransformationGraph.from_manifest(
            manifest(
                current="train",
                snapshots=(
                    snapshot("unused", "a" * 64, SnapshotRole.source),
                    snapshot("raw", "b" * 64, SnapshotRole.intermediate),
                    snapshot("train", "c" * 64, SnapshotRole.train),
                ),
                steps=(step("prep", TransformationOperation.filter, ("raw",), ("train",)),),
            )
        )
    with pytest.raises(ValueError, match="cycle"):
        TransformationGraph.from_manifest(
            manifest(
                current="mid",
                snapshots=(snapshot("mid", "a" * 64, SnapshotRole.intermediate),),
                steps=(
                    step("loop", TransformationOperation.derive, ("mid",), ("mid",)),
                ),
            )
        )


def test_topological_order_is_stable_across_input_ordering() -> None:
    snapshots = (
        snapshot("z-raw", "a" * 64, SnapshotRole.source),
        snapshot("m-mid", "b" * 64, SnapshotRole.intermediate),
        snapshot("a-train", "c" * 64, SnapshotRole.train),
    )
    steps = (
        step("z-split", TransformationOperation.split, ("z-raw",), ("m-mid",)),
        step("a-aug", TransformationOperation.augment, ("m-mid",), ("a-train",)),
    )
    first = TransformationGraph.from_manifest(
        manifest(current="a-train", snapshots=snapshots, steps=steps)
    )
    second = TransformationGraph.from_manifest(
        manifest(
            current="a-train",
            snapshots=tuple(reversed(snapshots)),
            steps=tuple(reversed(steps)),
        )
    )
    assert first.topological_steps() == second.topological_steps() == ("z-split", "a-aug")
    assert first.topological_snapshots() == second.topological_snapshots()


def test_ancestor_and_precedence_queries_for_branches_and_joins() -> None:
    graph = TransformationGraph.from_manifest(
        manifest(
            current="train",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("left", "b" * 64, SnapshotRole.intermediate),
                snapshot("right", "c" * 64, SnapshotRole.intermediate),
                snapshot("joined", "d" * 64, SnapshotRole.intermediate),
                snapshot("train", "e" * 64, SnapshotRole.train),
                snapshot("held", "f" * 64, SnapshotRole.test),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("left", "right")),
                step("join", TransformationOperation.join, ("left", "right"), ("joined",)),
                step("fit", TransformationOperation.augment, ("joined",), ("train",)),
                step("hold", TransformationOperation.sample, ("raw",), ("held",)),
            ),
        )
    )
    assert graph.snapshot_ancestors("train") == ("raw", "left", "right", "joined")
    snapshots, steps = graph.lineage_to("train")
    assert "held" not in snapshots
    assert "hold" not in steps
    assert graph.step_precedes("split", "fit")
    assert not graph.step_precedes("fit", "split")
    assert not graph.step_precedes("split", "split")
    assert graph.step_inputs("join") == ("left", "right")
    assert graph.producer_of("raw") is None
    assert graph.producer_of("train") == "fit"
    assert graph.source_ids() == ("raw",)
    assert graph.augment_steps() == ("fit",)
    assert graph.split_steps() == ("split",)
    assert graph.evaluation_snapshot_ids() == ("held",)
    assert graph.snapshot("raw").role is SnapshotRole.source
    assert graph.step("join").operation is TransformationOperation.join


def test_step_precedes_explores_reconverging_branches() -> None:
    graph = TransformationGraph.from_manifest(
        manifest(
            current="final",
            snapshots=(
                snapshot("raw", "a" * 64, SnapshotRole.source),
                snapshot("left", "b" * 64, SnapshotRole.intermediate),
                snapshot("right", "c" * 64, SnapshotRole.intermediate),
                snapshot("left2", "d" * 64, SnapshotRole.intermediate),
                snapshot("right2", "e" * 64, SnapshotRole.intermediate),
                snapshot("train", "f" * 64, SnapshotRole.train),
                snapshot("final", "aa" * 32, SnapshotRole.train),
            ),
            steps=(
                step("split", TransformationOperation.split, ("raw",), ("left", "right")),
                step("al", TransformationOperation.filter, ("left",), ("left2",)),
                step("ar", TransformationOperation.filter, ("right",), ("right2",)),
                step("join", TransformationOperation.join, ("left2", "right2"), ("train",)),
                step("fit", TransformationOperation.sample, ("train",), ("final",)),
            ),
        )
    )
    assert graph.step_precedes("split", "fit")
    assert graph.step_precedes("split", "join")
    assert not graph.step_precedes("al", "ar")


def test_query_helpers_reject_unknown_ids() -> None:
    graph = _linear()
    with pytest.raises(KeyError):
        graph.snapshot_ancestors("missing")
    with pytest.raises(KeyError):
        graph.lineage_to("missing")
    with pytest.raises(KeyError):
        graph.step_precedes("missing", "augment")
    with pytest.raises(KeyError):
        graph.step_precedes("augment", "missing")
