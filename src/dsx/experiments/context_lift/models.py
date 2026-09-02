"""Pydantic contracts for the frozen Context Lift experiment.

These models are the normative source for persisted pilot data and the
structured model response. JSON Schema is generated from them when needed.
"""

from __future__ import annotations

import hashlib
import json
import random
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PilotContract(BaseModel):
    """Base configuration for persisted pilot contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


ResponseSchemaName = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
]


class Arm(StrEnum):
    packet_off = "packet_off"
    packet_on = "packet_on"


class OutcomeKind(StrEnum):
    transport_error = "transport_error"
    provider_error = "provider_error"
    refused = "refused"
    incomplete = "incomplete"
    invalid_output = "invalid_output"
    completed = "completed"


class Metric(StrEnum):
    """Metrics a model can select for the pilot evaluation."""

    recall_at_5_percent = "recall_at_5_percent"
    precision_at_5_percent = "precision_at_5_percent"
    pr_auc = "pr_auc"
    accuracy = "accuracy"


class PilotRow(PilotContract):
    """One labeled fixture row with controlled signal and noise fields."""

    row_id: str = Field(min_length=1)
    label: Literal[0, 1]
    signal_a: float
    signal_b: float
    noise: float
    category: str = Field(min_length=1)
    nullable_numeric: float | None


class PilotCase(PilotContract):
    """A reproducibly generated pilot task and its rows."""

    generation_seed: int
    task_text: str = Field(min_length=1)
    rows: tuple[PilotRow, ...] = Field(min_length=1)
    manual_review_fraction: float = Field(default=0.05, ge=0.05, le=0.05)


class RunManifest(PilotContract):
    """Immutable commitments and intended scope published before live pairs run."""

    case_digest: str = Field(min_length=64, max_length=64)
    packet_version: str = Field(min_length=1)
    model_identifier: str = Field(min_length=1)
    generation_seed: int
    base_order_seed: int
    intended_pair_count: Literal[3] = 3
    pair_ids: tuple[str, ...] = Field(min_length=3, max_length=3)
    pair_order_seeds: tuple[int, ...] = Field(min_length=3, max_length=3)
    packet_off_request_digest: str = Field(min_length=64, max_length=64)
    packet_on_request_digest: str = Field(min_length=64, max_length=64)
    common_projection_digest: str = Field(min_length=64, max_length=64)
    claim_label: Literal["one-case unscored information-availability pilot"] = (
        "one-case unscored information-availability pilot"
    )


class DatasetShape(PilotContract):
    rows: int = Field(gt=0)
    columns: int = Field(gt=0)


class ColumnFact(PilotContract):
    name: str = Field(min_length=1)
    likely_id: bool
    missingness: float = Field(ge=0.0, le=1.0)


class ClassCount(PilotContract):
    """An immutable exact count for one binary class."""

    label: Literal[0, 1]
    count: int = Field(ge=0)


class ClassRate(PilotContract):
    """An immutable observed rate for one binary class."""

    label: Literal[0, 1]
    rate: float = Field(ge=0.0, le=1.0)


class Packet(PilotContract):
    """Evidence profile made available only in the packet-on treatment."""

    version: str = Field(min_length=1)
    dataset_shape: DatasetShape
    class_counts: tuple[ClassCount, ...] = Field(min_length=1)
    class_rates: tuple[ClassRate, ...] = Field(min_length=1)
    majority_baseline_accuracy: float = Field(ge=0.0, le=1.0)
    column_facts: tuple[ColumnFact, ...] = Field(min_length=1)
    metric_guidance: str = Field(min_length=1)
    split_guidance: str = Field(min_length=1)
    exclusions: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence_references: tuple[str, ...]


PILOT_CASE_TASK_TEXT = (
    "You are designing a classifier that ranks cases for manual review. Only 5% of "
    "cases can be reviewed. Recommend the primary metric, supporting metrics, split "
    "strategy, feature exclusions, and modeling approach. Explain limitations and cite "
    "any packet facts you use."
)
FROZEN_CASE_DIGEST = "57eec293b8b511ac9c1cf244eded3dddd483f375b47435bd92db67dcc5af5c9a"


def generate_pilot_case(seed: int = 20260819) -> PilotCase:
    """Generate the fixed-size synthetic case with seed-controlled positive positions."""
    positive_indices = set(random.Random(seed).sample(range(5_000), 100))
    categories = ("north", "south", "east", "west")
    rows = tuple(
        PilotRow(
            row_id=f"case-{index:05d}",
            label=1 if index in positive_indices else 0,
            signal_a=float(
                (1 if index in positive_indices else 0) * 1_000 + (index * 17) % 100
            ),
            signal_b=float(
                (1 if index in positive_indices else 0) * 2_000 + (index * 29) % 100
            ),
            noise=float((index * 37 + 13) % 100),
            category=categories[index % 4],
            nullable_numeric=None if index % 20 == 0 else float((index * 43) % 100),
        )
        for index in range(5_000)
    )
    return PilotCase(generation_seed=seed, task_text=PILOT_CASE_TASK_TEXT, rows=rows)


def canonical_case_digest(case: PilotCase) -> str:
    """Return the canonical SHA-256 digest for a complete persisted pilot case."""
    payload = json.dumps(
        case.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def candidate_packet() -> Packet:
    """Return the hand-authored packet available only to the packet-on treatment."""
    return Packet(
        version="pilot-v1",
        dataset_shape=DatasetShape(rows=5_000, columns=7),
        class_counts=(ClassCount(label=0, count=4_900), ClassCount(label=1, count=100)),
        class_rates=(ClassRate(label=0, rate=0.98), ClassRate(label=1, rate=0.02)),
        majority_baseline_accuracy=0.98,
        column_facts=(
            ColumnFact(name="row_id", likely_id=True, missingness=0.0),
            ColumnFact(name="label", likely_id=False, missingness=0.0),
            ColumnFact(name="signal_a", likely_id=False, missingness=0.0),
            ColumnFact(name="signal_b", likely_id=False, missingness=0.0),
            ColumnFact(name="noise", likely_id=False, missingness=0.0),
            ColumnFact(name="category", likely_id=False, missingness=0.0),
            ColumnFact(name="nullable_numeric", likely_id=False, missingness=0.05),
        ),
        metric_guidance=(
            "Use recall at the top 5% as the primary metric; precision at 5% and PR-AUC "
            "are supporting metrics. Accuracy cannot stand alone."
        ),
        split_guidance=(
            "Use a stratified validation design and preserve the 5% ranking objective."
        ),
        exclusions=("row_id",),
        limitations=(
            "This is one deterministic synthetic, unscored information-availability case.",
            "The packet-off arm has no equivalent discovery access.",
        ),
        evidence_references=(
            "Fact: row count is 5,000.",
            "Fact: class distribution is 4,900 label-0 rows and 100 label-1 rows.",
            "Fact: row-ID uniqueness covers case-00000 through case-04999.",
            "Fact: nullable missingness is 0.05 at indices divisible by 20.",
        ),
    )


class AnalysisDecision(PilotContract):
    """Structured response requested from a model for later evaluation."""

    primary_metric: Metric
    supporting_metrics: tuple[Metric, ...]
    review_budget_fraction: float = Field(ge=0.05, le=0.05)
    split_strategy: str = Field(min_length=1)
    excluded_columns: tuple[str, ...]
    reasoning: str = Field(min_length=1)
    limitations: tuple[str, ...]
    recommendation: str = Field(min_length=1)
    packet_citations: tuple[str, ...]

    @property
    def excludes_row_id(self) -> bool:
        """Whether the model's observable decision excludes the row identifier."""
        return "row_id" in self.excluded_columns


class RequestContext(PilotContract):
    """Treatment context; profile_packet is intentionally its sole field."""

    profile_packet: Packet | None


class ModelRequest(PilotContract):
    """Exact request passed to a later ModelClient implementation."""

    model_identifier: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    task_prompt: str = Field(min_length=1)
    response_schema_name: ResponseSchemaName
    context: RequestContext


class ArmOutcome(PilotContract):
    """A single arm's result with outcome classification and diagnostics."""

    arm: Arm
    outcome_kind: OutcomeKind
    request_digest: str = Field(min_length=1)
    common_projection_digest: str = Field(min_length=1)
    attempt_number: int = Field(gt=0)
    request_number: int = Field(gt=0)
    parsed_decision: AnalysisDecision | None = None
    raw_response: str | None = None
    diagnostic: str | None = None

    @model_validator(mode="after")
    def validate_completed_consistency(self) -> ArmOutcome:
        if self.outcome_kind is OutcomeKind.completed and self.parsed_decision is None:
            raise ValueError("completed outcomes require parsed_decision")
        if self.outcome_kind is not OutcomeKind.completed and self.parsed_decision is not None:
            raise ValueError("non-completed outcomes must not include parsed_decision")
        return self


class AttemptStart(PilotContract):
    """Immutable metadata published before a pair-attempt makes a client call."""

    pair_number: int = Field(gt=0)
    pair_id: str = Field(min_length=1)
    attempt_number: int = Field(gt=0)
    attempt_id: str = Field(min_length=1)
    arm_order: tuple[Arm, Arm]
    common_projection_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_distinct_arms(self) -> AttemptStart:
        if len(set(self.arm_order)) != 2:
            raise ValueError("attempt arm_order must contain both distinct arms")
        return self


class AttemptTerminalStatus(StrEnum):
    complete = "complete"
    infra_failure = "infra_failure"


class AttemptSummary(PilotContract):
    """Append-only terminal record for one fully executed pair attempt."""

    start: AttemptStart
    outcomes: tuple[ArmOutcome, ...]
    terminal_status: AttemptTerminalStatus

    @model_validator(mode="after")
    def validate_outcomes(self) -> AttemptSummary:
        seen: set[tuple[Arm, int]] = set()
        for outcome in self.outcomes:
            if outcome.attempt_number != self.start.attempt_number:
                raise ValueError("outcome attempt number must match attempt start")
            # Arm has two members and a valid start contains both. This remains a
            # fail-closed defense for unchecked in-memory construction.
            if outcome.arm not in self.start.arm_order:  # pragma: no cover
                raise ValueError("outcome arm must be in attempt arm_order")
            if outcome.common_projection_digest != self.start.common_projection_digest:
                raise ValueError("outcome common projection digest must match attempt start")
            key = (outcome.arm, outcome.request_number)
            if key in seen:
                raise ValueError("duplicate outcome arm/request number")
            seen.add(key)
        arm_groups: list[tuple[Arm, list[ArmOutcome]]] = []
        for outcome in self.outcomes:
            if not arm_groups or arm_groups[-1][0] is not outcome.arm:
                arm_groups.append((outcome.arm, []))
            arm_groups[-1][1].append(outcome)
        if tuple(arm for arm, _ in arm_groups) != self.start.arm_order[: len(arm_groups)]:
            raise ValueError("outcomes must follow arm order without interleaving")
        infrastructure = {OutcomeKind.transport_error, OutcomeKind.provider_error}
        for _arm, outcomes in arm_groups:
            request_numbers = [outcome.request_number for outcome in outcomes]
            if request_numbers != list(range(1, len(outcomes) + 1)):
                raise ValueError("per-arm request numbers must be contiguous from one")
            if len(outcomes) > 3:
                raise ValueError("an arm may have at most three requests")
            if any(outcome.outcome_kind not in infrastructure for outcome in outcomes[:-1]):
                raise ValueError("terminal non-infrastructure outcome must end its arm")
            last = outcomes[-1]
            if last.outcome_kind in infrastructure and (
                len(outcomes) != 3 or last.request_number != 3
            ):
                raise ValueError("infrastructure exhaustion requires request three")
        if self.terminal_status is AttemptTerminalStatus.complete:
            if len(arm_groups) != 2 or any(
                outcomes[-1].outcome_kind in infrastructure for _, outcomes in arm_groups
            ):
                raise ValueError("complete attempt requires both terminal non-infrastructure arms")
        else:
            if any(
                outcomes[-1].outcome_kind in infrastructure
                for _, outcomes in arm_groups[:-1]
            ):
                raise ValueError("only the last executed arm may exhaust")
            if (
                len(arm_groups) not in {1, 2}
                or arm_groups[-1][1][-1].outcome_kind not in infrastructure
            ):
                raise ValueError("infra failure requires the last executed arm to exhaust")
        return self


class PairOutcomeKind(StrEnum):
    complete = "complete"
    infra_incomplete = "infra_incomplete"


class PairSummary(PilotContract):
    """The one terminal record for a run's paired state machine."""

    pair_number: int = Field(gt=0)
    pair_id: str = Field(min_length=1)
    outcome_kind: PairOutcomeKind
    attempts: tuple[AttemptSummary, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_attempt_ledger(self) -> PairSummary:
        ids: set[str] = set()
        for expected, attempt in enumerate(self.attempts, start=1):
            start = attempt.start
            if start.pair_number != self.pair_number or start.pair_id != self.pair_id:
                raise ValueError("attempt identity must match pair summary")
            if start.attempt_number != expected:
                raise ValueError("attempt numbers must be ordered from one")
            if start.attempt_id in ids:
                raise ValueError("attempt IDs must be unique")
            ids.add(start.attempt_id)
        final_status = self.attempts[-1].terminal_status
        expected_status = (
            AttemptTerminalStatus.complete
            if self.outcome_kind is PairOutcomeKind.complete
            else AttemptTerminalStatus.infra_failure
        )
        if final_status is not expected_status:
            raise ValueError("pair outcome kind must match final attempt terminal status")
        if (
            len(self.attempts) == 2
            and self.attempts[0].terminal_status is not AttemptTerminalStatus.infra_failure
        ):
            raise ValueError("a retry pair requires an initial infrastructure failure")
        if self.outcome_kind is PairOutcomeKind.infra_incomplete and (
            len(self.attempts) != 2
            or any(
                attempt.terminal_status is not AttemptTerminalStatus.infra_failure
                for attempt in self.attempts
            )
        ):
            raise ValueError("infra incomplete requires two infrastructure-failure attempts")
        return self


class BlindExclusion(PilotContract):
    """A non-identifying count of terminal pairs excluded from blind review."""

    reason: str = Field(min_length=1)
    count: int = Field(gt=0)


class BlindManifest(PilotContract):
    """Public, non-identifying index for an opaque blind-evaluation bundle."""

    version: str = Field(min_length=1)
    blind_seed: int
    ordered_opaque_ids: tuple[str, ...]
    eligible_count: int = Field(ge=0)
    exclusions: tuple[BlindExclusion, ...]
    output_digests: tuple[BlindOutputDigest, ...]

    @model_validator(mode="after")
    def validate_ordered_ids(self) -> BlindManifest:
        if len(self.ordered_opaque_ids) != self.eligible_count:
            raise ValueError("eligible_count must equal ordered opaque ID count")
        if len(set(self.ordered_opaque_ids)) != len(self.ordered_opaque_ids):
            raise ValueError("ordered opaque IDs must be unique")
        if tuple(item.opaque_id for item in self.output_digests) != self.ordered_opaque_ids:
            raise ValueError("output digests must be in opaque ID order")
        return self


class BlindOutputDigest(PilotContract):
    """Non-identifying content commitment for a public opaque output."""

    opaque_id: str = Field(min_length=1)
    digest: str = Field(min_length=1)


class PublicBlindOutput(PilotContract):
    """The only decision data published to a blind evaluator for one output."""

    opaque_id: str = Field(min_length=1)
    decision: AnalysisDecision


class BlindJudgment(PilotContract):
    """One evaluator judgment, kept separate from official treatment assignment."""

    opaque_id: str = Field(min_length=1)
    decision_quality: int = Field(ge=1, le=5, strict=True)
    evidence_use: int = Field(ge=1, le=5, strict=True)
    limitations_quality: int = Field(ge=1, le=5, strict=True)
    packet_guess: Arm
    guess_confidence: int = Field(ge=1, le=5, strict=True)
    metric_reasoning: int = Field(ge=1, le=5, strict=True)
    split_strategy: int = Field(ge=1, le=5, strict=True)
    leakage_row_id_avoidance: int = Field(ge=1, le=5, strict=True)
    limitations: int = Field(ge=1, le=5, strict=True)
    overall_recommendation_quality: int = Field(ge=1, le=5, strict=True)
    exact_prevalence_recognition: int = Field(ge=1, le=5, strict=True)
    majority_baseline_recognition: int = Field(ge=1, le=5, strict=True)
    citation_use: int = Field(ge=1, le=5, strict=True)


class FrozenJudgments(PilotContract):
    """Immutable, manifest-bound judgment set used as the reveal gate."""

    manifest_digest: str = Field(min_length=1)
    judgments_digest: str = Field(min_length=1)
    judgments: tuple[BlindJudgment, ...]


class RevealEntry(PilotContract):
    """Official treatment identity revealed only after frozen judgments validate."""

    opaque_id: str = Field(min_length=1)
    pair_number: int = Field(gt=0)
    pair_id: str = Field(min_length=1)
    arm: Arm


class RevealMap(PilotContract):
    """Post-freeze join from opaque IDs to the persisted pair treatment labels."""

    manifest_digest: str = Field(min_length=1)
    entries: tuple[RevealEntry, ...]


class ArmComparativeRatings(PilotContract):
    """Fair-comparison ratings that deliberately exclude packet-only diagnostics."""

    arm: Arm
    count: int = Field(ge=0)
    decision_quality: float
    evidence_use: float
    limitations_quality: float
    metric_reasoning: float
    split_strategy: float
    leakage_row_id_avoidance: float
    limitations: float
    overall_recommendation_quality: float


class PacketUptakeRatings(PilotContract):
    """Packet-uptake diagnostics reported outside fair comparative ratings."""

    arm: Arm
    count: int = Field(ge=0)
    exact_prevalence_recognition: float
    majority_baseline_recognition: float
    citation_use: float


class ArmGuessResult(PilotContract):
    """Post-reveal agreement between a blind arm guess and official arm identity."""

    arm: Arm
    count: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    accuracy: float = Field(ge=0.0, le=1.0)
    mean_confidence: float = Field(ge=0.0, le=5.0)


class RevealedJudgment(PilotContract):
    """Raw frozen judgment paired with its official label after reveal."""

    opaque_id: str = Field(min_length=1)
    pair_number: int = Field(gt=0)
    pair_id: str = Field(min_length=1)
    arm: Arm
    judgment: BlindJudgment


class RevealedReport(PilotContract):
    """One-case descriptive blind-evaluation report without causal inference."""

    raw_judgments: tuple[RevealedJudgment, ...]
    comparative_ratings: tuple[ArmComparativeRatings, ...]
    packet_uptake_diagnostics: tuple[PacketUptakeRatings, ...]
    arm_guess_results: tuple[ArmGuessResult, ...]
