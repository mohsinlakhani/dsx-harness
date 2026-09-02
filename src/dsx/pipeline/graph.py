"""In-memory bipartite graph derivation, validation, and traversal."""

from __future__ import annotations

from collections import defaultdict, deque

from .models import (
    DatasetSnapshot,
    SnapshotRole,
    TransformationManifest,
    TransformationOperation,
    TransformationStep,
)


class TransformationGraph:
    """Validated snapshot/step graph derived from a transformation manifest."""

    def __init__(self, manifest: TransformationManifest) -> None:
        self.manifest = manifest
        self._snapshots = {snapshot.snapshot_id: snapshot for snapshot in manifest.snapshots}
        self._steps = {step.step_id: step for step in manifest.steps}
        self._producer: dict[str, str] = {}
        self._step_inputs: dict[str, tuple[str, ...]] = {}
        self._step_outputs: dict[str, tuple[str, ...]] = {}
        self._consumers: dict[str, tuple[str, ...]] = {}
        self._step_successors: dict[str, tuple[str, ...]] = {}
        self._step_predecessors: dict[str, tuple[str, ...]] = {}
        self._topological_steps: tuple[str, ...] = ()
        self._topological_snapshots: tuple[str, ...] = ()
        self._validate_and_index()

    @classmethod
    def from_manifest(cls, manifest: TransformationManifest) -> TransformationGraph:
        """Validate a manifest and return its derived graph."""
        return cls(manifest)

    def snapshot(self, snapshot_id: str) -> DatasetSnapshot:
        return self._snapshots[snapshot_id]

    def step(self, step_id: str) -> TransformationStep:
        return self._steps[step_id]

    def topological_steps(self) -> tuple[str, ...]:
        """Return step IDs in a stable topological order."""
        return self._topological_steps

    def topological_snapshots(self) -> tuple[str, ...]:
        """Return snapshot IDs in a stable topological order."""
        return self._topological_snapshots

    def snapshot_ancestors(self, snapshot_id: str) -> tuple[str, ...]:
        """Return ancestor snapshot IDs of ``snapshot_id`` in topological order."""
        if snapshot_id not in self._snapshots:
            raise KeyError(snapshot_id)
        ancestors: set[str] = set()
        pending = deque([snapshot_id])
        seen = {snapshot_id}
        while pending:
            current = pending.popleft()
            producer = self._producer.get(current)
            if producer is None:
                continue
            for source in self._step_inputs[producer]:
                if source not in seen:
                    seen.add(source)
                    ancestors.add(source)
                    pending.append(source)
        return tuple(
            snapshot_id_
            for snapshot_id_ in self._topological_snapshots
            if snapshot_id_ in ancestors
        )

    def lineage_to(self, snapshot_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return snapshots and steps on lineage from sources to ``snapshot_id``."""
        if snapshot_id not in self._snapshots:
            raise KeyError(snapshot_id)
        lineage_snapshots = set(self.snapshot_ancestors(snapshot_id))
        lineage_snapshots.add(snapshot_id)
        lineage_steps = {
            self._producer[item]
            for item in lineage_snapshots
            if item in self._producer
        }
        return (
            tuple(item for item in self._topological_snapshots if item in lineage_snapshots),
            tuple(item for item in self._topological_steps if item in lineage_steps),
        )

    def step_precedes(self, earlier_step_id: str, later_step_id: str) -> bool:
        """Return whether ``earlier_step_id`` is upstream of ``later_step_id`` on any path."""
        if earlier_step_id not in self._steps or later_step_id not in self._steps:
            raise KeyError(f"{earlier_step_id}->{later_step_id}")
        if earlier_step_id == later_step_id:
            return False
        pending = deque([earlier_step_id])
        seen = {earlier_step_id}
        while pending:
            current = pending.popleft()
            for successor in self._step_successors[current]:
                if successor == later_step_id:
                    return True
                if successor not in seen:
                    seen.add(successor)
                    pending.append(successor)
        return False

    def step_inputs(self, step_id: str) -> tuple[str, ...]:
        return self._step_inputs[step_id]

    def step_outputs(self, step_id: str) -> tuple[str, ...]:
        return self._step_outputs[step_id]

    def producer_of(self, snapshot_id: str) -> str | None:
        return self._producer.get(snapshot_id)

    def source_ids(self) -> tuple[str, ...]:
        return tuple(
            snapshot.snapshot_id
            for snapshot in self.manifest.snapshots
            if snapshot.role is SnapshotRole.source
        )

    def augment_steps(self) -> tuple[str, ...]:
        return tuple(
            step_id
            for step_id in self._topological_steps
            if self._steps[step_id].operation is TransformationOperation.augment
        )

    def split_steps(self) -> tuple[str, ...]:
        return tuple(
            step_id
            for step_id in self._topological_steps
            if self._steps[step_id].operation is TransformationOperation.split
        )

    def evaluation_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(
            snapshot_id
            for snapshot_id in self._topological_snapshots
            if self._snapshots[snapshot_id].role
            in {SnapshotRole.validation, SnapshotRole.test}
        )

    def _validate_and_index(self) -> None:
        snapshot_ids = [snapshot.snapshot_id for snapshot in self.manifest.snapshots]
        if len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("duplicate snapshot IDs")
        step_ids = [step.step_id for step in self.manifest.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("duplicate step IDs")
        if self.manifest.current_snapshot_id not in self._snapshots:
            raise ValueError("current snapshot is missing")

        consumers: dict[str, list[str]] = defaultdict(list)
        for step in self.manifest.steps:
            self._step_inputs[step.step_id] = step.inputs
            self._step_outputs[step.step_id] = step.outputs
            for snapshot_id in (*step.inputs, *step.outputs):
                if snapshot_id not in self._snapshots:
                    raise ValueError(
                        f"step {step.step_id} references missing snapshot {snapshot_id}"
                    )
            for output_id in step.outputs:
                if output_id in self._producer:
                    raise ValueError(f"snapshot {output_id} has more than one producer")
                self._producer[output_id] = step.step_id
            for input_id in step.inputs:
                consumers[input_id].append(step.step_id)
        self._consumers = {snapshot_id: tuple(ids) for snapshot_id, ids in consumers.items()}

        for snapshot in self.manifest.snapshots:
            producer = self._producer.get(snapshot.snapshot_id)
            if snapshot.role is SnapshotRole.source and producer is not None:
                raise ValueError(f"source snapshot {snapshot.snapshot_id} has a producer")

        successors: dict[str, set[str]] = {step_id: set() for step_id in self._steps}
        predecessors: dict[str, set[str]] = {step_id: set() for step_id in self._steps}
        for step in self.manifest.steps:
            for output_id in step.outputs:
                for consumer in self._consumers.get(output_id, ()):
                    successors[step.step_id].add(consumer)
                    predecessors[consumer].add(step.step_id)
        self._step_successors = {
            step_id: tuple(sorted(ids)) for step_id, ids in successors.items()
        }
        self._step_predecessors = {
            step_id: tuple(sorted(ids)) for step_id, ids in predecessors.items()
        }

        self._topological_steps = self._stable_step_order(predecessors)
        self._topological_snapshots = self._stable_snapshot_order()
        if not self._reachable_from_source(self.manifest.current_snapshot_id):
            raise ValueError("current snapshot is not reachable from a source")
        for snapshot in self.manifest.snapshots:
            producer = self._producer.get(snapshot.snapshot_id)
            if snapshot.role is not SnapshotRole.source and producer is None:
                raise ValueError(f"non-source snapshot {snapshot.snapshot_id} has no producer")

    def _stable_step_order(self, predecessors: dict[str, set[str]]) -> tuple[str, ...]:
        remaining = {step_id: set(ids) for step_id, ids in predecessors.items()}
        ready = sorted(step_id for step_id, ids in remaining.items() if not ids)
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for successor in self._step_successors[current]:
                remaining[successor].discard(current)
                if remaining[successor] or successor in ordered or successor in ready:
                    continue
                ready.append(successor)
                ready.sort()
        if len(ordered) != len(self._steps):
            raise ValueError("transformation graph contains a cycle")
        return tuple(ordered)

    def _stable_snapshot_order(self) -> tuple[str, ...]:
        remaining_inputs = {
            step_id: set(self._step_inputs[step_id]) for step_id in self._steps
        }
        ready = sorted(
            snapshot.snapshot_id
            for snapshot in self.manifest.snapshots
            if snapshot.snapshot_id not in self._producer
        )
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for step_id in self._consumers.get(current, ()):
                remaining_inputs[step_id].discard(current)
                if remaining_inputs[step_id]:
                    continue
                for output_id in sorted(self._step_outputs[step_id]):
                    already_ready = output_id in ordered or output_id in ready
                    if already_ready:  # pragma: no cover - unique producers
                        continue
                    ready.append(output_id)
                ready.sort()
        if len(ordered) != len(self._snapshots):  # pragma: no cover - step cycle is detected first
            raise ValueError("transformation graph contains a cycle")
        return tuple(ordered)

    def _reachable_from_source(self, snapshot_id: str) -> bool:
        sources = [
            snapshot.snapshot_id
            for snapshot in self.manifest.snapshots
            if snapshot.role is SnapshotRole.source
        ]
        pending = deque(sources)
        seen = set(sources)
        while pending:
            current = pending.popleft()
            if current == snapshot_id:
                return True
            for step_id in self._consumers.get(current, ()):
                for output_id in self._step_outputs[step_id]:
                    if output_id not in seen:
                        seen.add(output_id)
                        pending.append(output_id)
        return False
