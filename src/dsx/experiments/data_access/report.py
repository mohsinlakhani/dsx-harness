"""Descriptive Data Access metrics assembled from immutable execution ledgers.

The execution package intentionally need not inherit any report protocol.  These
functions accept either mappings or objects with documented structural attributes:
``arm``, ``repetition_id``, ``repetition_number``, ``elapsed_seconds``, ``model_events`` (or
``model_calls``), and ``sql_attempts``.  A model event may expose a nested ``usage``
object or its token fields directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import mean, median
from typing import Any, Literal

from pydantic import Field

from .evaluation import (
    ClaimStatus,
    DecisionEvaluation,
    EvidenceKind,
    SqlReplay,
    SqlReplayStatus,
    sql_attempts_from_ledger,
)
from .models import (
    Arm,
    DataAccessContract,
    DataAccessManifest,
    PacketBuildMetrics,
    PricingSnapshot,
    SqlAttemptOutcome,
)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _sequence(item: Any, *names: str) -> Sequence[Any]:
    for name in names:
        value = _value(item, name)
        if value is not None:
            if isinstance(value, Sequence) and not isinstance(value, str | bytes):
                return value
            raise ValueError(f"ledger field {name} must be a sequence")
    return ()


def _float(item: Any, name: str, default: float = 0.0) -> float:
    value = _value(item, name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"ledger field {name} must be numeric")
    return float(value)


def _int(item: Any, name: str, default: int = 0) -> int:
    value = _value(item, name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"ledger field {name} must be an integer")
    return value


class TokenUsage(DataAccessContract):
    """Provider-neutral token buckets; input/output totals include their sub-buckets."""

    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


def _nested(item: Any, *names: str) -> Any:
    current = item
    for name in names:
        current = _value(current, name)
        if current is None:
            return None
    return current


def token_usage_from_event(event: Any) -> TokenUsage:
    """Normalize OpenAI-style and provider-neutral usage objects from a model event."""
    usage = _value(event, "usage", event)
    input_tokens = _int(usage, "input_tokens")
    cached = _int(usage, "cached_input_tokens")
    if cached == 0:
        details = _nested(usage, "input_tokens_details")
        if details is not None:
            cached = _int(details, "cached_tokens")
    output_tokens = _int(usage, "output_tokens")
    reasoning = _int(usage, "reasoning_tokens")
    if reasoning == 0:
        details = _nested(usage, "output_tokens_details")
        if details is not None:
            reasoning = _int(details, "reasoning_tokens")
    total = _int(usage, "total_tokens", input_tokens + output_tokens)
    if cached > input_tokens:
        raise ValueError("cached input tokens cannot exceed input tokens")
    if reasoning > output_tokens:
        raise ValueError("reasoning tokens cannot exceed output tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        total_tokens=total,
    )


def estimated_cost_usd(usage: TokenUsage, pricing: PricingSnapshot) -> float:
    """Estimate cost from the frozen snapshot without double-pricing sub-buckets."""
    uncached_input = usage.input_tokens - usage.cached_input_tokens
    visible_output = usage.output_tokens - usage.reasoning_tokens
    return (
        uncached_input * pricing.input.usd_per_million_tokens
        + usage.cached_input_tokens * pricing.cached_input.usd_per_million_tokens
        + visible_output * pricing.output.usd_per_million_tokens
        + usage.reasoning_tokens * pricing.reasoning.usd_per_million_tokens
    ) / 1_000_000


class OutcomeCount(DataAccessContract):
    outcome: SqlAttemptOutcome
    count: int = Field(ge=0)


class ClaimCounts(DataAccessContract):
    supported: int = Field(default=0, ge=0)
    unsupported: int = Field(default=0, ge=0)
    contradicted: int = Field(default=0, ge=0)
    unverifiable: int = Field(default=0, ge=0)


class EvidenceMetrics(DataAccessContract):
    references: int = Field(default=0, ge=0)
    resolved_references: int = Field(default=0, ge=0)
    invalid_references: int = Field(default=0, ge=0)
    resolution_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    packet_pointers: int = Field(default=0, ge=0)
    resolved_packet_pointers: int = Field(default=0, ge=0)
    cited_sql_replays: int = Field(default=0, ge=0)
    matched_sql_replays: int = Field(default=0, ge=0)
    sql_replay_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    packet_computation_replay: str = "not_applicable"


class AmortizedCost(DataAccessContract):
    reuses: int = Field(gt=0)
    total_cost_usd: float = Field(ge=0.0)


class ArmMetrics(DataAccessContract):
    arm: Arm
    repetition_id: str = Field(min_length=1)
    repetition_number: int = Field(gt=0)
    elapsed_seconds: float = Field(ge=0.0)
    model_latency_seconds: float = Field(ge=0.0)
    tool_latency_seconds: float = Field(ge=0.0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    successful_discovery_attempts: int = Field(ge=0)
    failed_discovery_attempts: int = Field(ge=0)
    failed_discovery_by_outcome: tuple[OutcomeCount, ...] = Field(default_factory=tuple)
    usage: TokenUsage
    inference_cost_usd: float = Field(ge=0.0)
    packet_build_cost_usd: float | None = Field(default=None, ge=0.0)
    amortized_dsx_costs: tuple[AmortizedCost, ...] = Field(default_factory=tuple)
    claims: ClaimCounts
    evidence: EvidenceMetrics
    descriptive_label: str = "descriptive"


def _claim_counts(evaluation: DecisionEvaluation | None) -> ClaimCounts:
    if evaluation is None:
        return ClaimCounts()
    counts = evaluation.counts
    return ClaimCounts(
        supported=counts[ClaimStatus.supported.value],
        unsupported=counts[ClaimStatus.unsupported.value],
        contradicted=counts[ClaimStatus.contradicted.value],
        unverifiable=counts[ClaimStatus.unverifiable.value],
    )


def _evidence_metrics(
    evaluation: DecisionEvaluation | None, sql_replays: Sequence[SqlReplay]
) -> EvidenceMetrics:
    if evaluation is None:
        return EvidenceMetrics()
    resolutions = [
        resolution
        for assessment in evaluation.assessments
        for resolution in assessment.evidence
    ]
    references = len(resolutions)
    resolved = evaluation.resolved_evidence_references
    packet = [
        resolution
        for resolution in resolutions
        if resolution.reference.kind is EvidenceKind.packet_json_pointer
    ]
    cited = len(sql_replays)
    matched = sum(replay.status is SqlReplayStatus.matched for replay in sql_replays)
    return EvidenceMetrics(
        references=references,
        resolved_references=resolved,
        invalid_references=evaluation.invalid_evidence_references,
        resolution_rate=resolved / references if references else None,
        packet_pointers=len(packet),
        resolved_packet_pointers=sum(
            resolution.status.value == "resolved" for resolution in packet
        ),
        cited_sql_replays=cited,
        matched_sql_replays=matched,
        sql_replay_rate=matched / cited if cited else None,
    )


def summarize_arm(
    ledger: Any,
    *,
    manifest: DataAccessManifest,
    evaluation: DecisionEvaluation | None = None,
    sql_replays: Sequence[SqlReplay] = (),
    packet_build_metrics: PacketBuildMetrics | None = None,
) -> ArmMetrics:
    """Summarize one arm ledger using the documented structural adapter contract."""
    arm = Arm(_value(ledger, "arm"))
    repetition_id = _value(ledger, "repetition_id")
    repetition_number = _value(ledger, "repetition_number")
    if not isinstance(repetition_id, str) or not isinstance(repetition_number, int):
        raise ValueError(
            "ledger must provide string repetition_id and integer repetition_number"
        )
    model_events = _sequence(ledger, "model_events", "model_calls")
    tool_events = _sequence(ledger, "sql_attempts", "tool_calls")
    direct_usage = _value(ledger, "usage")
    usage = (
        token_usage_from_event({"usage": direct_usage})
        if direct_usage is not None
        else TokenUsage()
    )
    if direct_usage is None:
        for event in model_events:
            response = _value(event, "response")
            usage += token_usage_from_event(
                {"usage": _value(response, "usage")} if response is not None else event
            )
    attempts = sql_attempts_from_ledger(tool_events)
    failures = tuple(
        OutcomeCount(outcome=outcome, count=sum(item.outcome is outcome for item in attempts))
        for outcome in SqlAttemptOutcome
        if outcome is not SqlAttemptOutcome.success
        and any(item.outcome is outcome for item in attempts)
    )
    build = packet_build_metrics or manifest.packet_build_metrics
    inference = estimated_cost_usd(usage, manifest.pricing)
    packet_cost = (
        build.estimated_cost_usd
        if arm in {Arm.dsx_packet, Arm.packet_and_full_data} and build is not None
        else None
    )
    amortized = (
        tuple(
            AmortizedCost(reuses=reuses, total_cost_usd=inference + packet_cost / reuses)
            for reuses in (1, 10, 100)
        )
        if packet_cost is not None
        else ()
    )
    return ArmMetrics(
        arm=arm,
        repetition_id=repetition_id,
        repetition_number=repetition_number,
        elapsed_seconds=_float(ledger, "elapsed_seconds"),
        model_latency_seconds=sum(_float(event, "elapsed_seconds") for event in model_events),
        tool_latency_seconds=sum(
            _float(event, "elapsed_seconds", attempt.elapsed_seconds)
            for event, attempt in zip(tool_events, attempts, strict=True)
        ),
        model_calls=len(model_events),
        tool_calls=len(attempts),
        successful_discovery_attempts=sum(
            attempt.outcome is SqlAttemptOutcome.success for attempt in attempts
        ),
        failed_discovery_attempts=sum(
            attempt.outcome is not SqlAttemptOutcome.success for attempt in attempts
        ),
        failed_discovery_by_outcome=failures,
        usage=usage,
        inference_cost_usd=inference,
        packet_build_cost_usd=packet_cost,
        amortized_dsx_costs=amortized,
        claims=_claim_counts(evaluation),
        evidence=_evidence_metrics(evaluation, sql_replays),
    )


class AggregateMetric(DataAccessContract):
    arm: Arm
    count: int = Field(ge=0)
    elapsed_seconds_mean: float = Field(ge=0.0)
    elapsed_seconds_median: float = Field(ge=0.0)
    inference_cost_usd_mean: float = Field(ge=0.0)
    inference_cost_usd_median: float = Field(ge=0.0)
    packet_build_cost_usd_mean: float | None = Field(default=None, ge=0.0)
    amortized_dsx_costs_mean: tuple[AmortizedCost, ...] = Field(default_factory=tuple)
    model_calls_mean: float = Field(ge=0.0)
    tool_calls_mean: float = Field(ge=0.0)
    unsupported_claims_mean: float = Field(ge=0.0)
    failed_discovery_attempts_mean: float = Field(ge=0.0)


class ArmContrast(DataAccessContract):
    """A directional within-repetition difference: ``left_arm - right_arm``."""

    repetition_id: str = Field(min_length=1)
    repetition_number: int = Field(gt=0)
    left_arm: Arm
    right_arm: Arm
    elapsed_seconds: float
    inference_cost_usd: float
    model_calls: int
    tool_calls: int
    unsupported_claims: int
    failed_discovery_attempts: int


class OperationalArmSummary(DataAccessContract):
    """All-attempt resource and infrastructure accounting, separate from efficacy."""

    arm: Arm
    attempted_arm_runs: int = Field(ge=0)
    completed_arm_runs: int = Field(ge=0)
    abandoned_arm_runs: int = Field(ge=0)
    total_elapsed_seconds: float = Field(ge=0.0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    usage: TokenUsage
    estimated_cost_usd: float = Field(ge=0.0)
    provider_error_calls: int = Field(ge=0)
    transport_error_calls: int = Field(ge=0)
    failed_sql_attempts: int = Field(ge=0)


class OperationalSummary(DataAccessContract):
    """Resource usage across every attempt, including replaced repetitions."""

    arm_summaries: tuple[OperationalArmSummary, ...]


class DataAccessReport(DataAccessContract):
    report_version: Literal["data-access-report-v2"]
    experiment_name: str = "Data Access v2"
    claim_label: str = "descriptive capability-ceiling comparison"
    raw_arm_metrics: tuple[ArmMetrics, ...]
    arm_aggregates: tuple[AggregateMetric, ...]
    arm_contrasts: tuple[ArmContrast, ...]
    operational: OperationalSummary


def _aggregate(arm: Arm, records: Sequence[ArmMetrics]) -> AggregateMetric:
    if not records:
        return AggregateMetric(
            arm=arm,
            count=0,
            elapsed_seconds_mean=0.0,
            elapsed_seconds_median=0.0,
            inference_cost_usd_mean=0.0,
            inference_cost_usd_median=0.0,
            packet_build_cost_usd_mean=None,
            amortized_dsx_costs_mean=(),
            model_calls_mean=0.0,
            tool_calls_mean=0.0,
            unsupported_claims_mean=0.0,
            failed_discovery_attempts_mean=0.0,
        )
    packet_build_costs = [
        record.packet_build_cost_usd
        for record in records
        if record.packet_build_cost_usd is not None
    ]
    amortized_means = tuple(
        AmortizedCost(
            reuses=reuses,
            total_cost_usd=mean(
                next(
                    cost.total_cost_usd
                    for cost in record.amortized_dsx_costs
                    if cost.reuses == reuses
                )
                for record in records
            ),
        )
        for reuses in (1, 10, 100)
    ) if packet_build_costs else ()
    return AggregateMetric(
        arm=arm,
        count=len(records),
        elapsed_seconds_mean=mean(record.elapsed_seconds for record in records),
        elapsed_seconds_median=median(record.elapsed_seconds for record in records),
        inference_cost_usd_mean=mean(record.inference_cost_usd for record in records),
        inference_cost_usd_median=median(record.inference_cost_usd for record in records),
        packet_build_cost_usd_mean=mean(packet_build_costs) if packet_build_costs else None,
        amortized_dsx_costs_mean=amortized_means,
        model_calls_mean=mean(record.model_calls for record in records),
        tool_calls_mean=mean(record.tool_calls for record in records),
        unsupported_claims_mean=mean(record.claims.unsupported for record in records),
        failed_discovery_attempts_mean=mean(
            record.failed_discovery_attempts for record in records
        ),
    )


def _ledger_usage(ledger: Any, model_events: Sequence[Any]) -> TokenUsage:
    direct_usage = _value(ledger, "usage")
    usage = (
        token_usage_from_event({"usage": direct_usage})
        if direct_usage is not None
        else TokenUsage()
    )
    if direct_usage is None:
        for event in model_events:
            response = _value(event, "response")
            usage += token_usage_from_event(
                {"usage": _value(response, "usage")} if response is not None else event
            )
    return usage


def summarize_operational_attempts(
    repetition_runs: Sequence[Any], *, manifest: DataAccessManifest
) -> OperationalSummary:
    """Aggregate every ``RepetitionRun.attempts[].arms`` record exactly once.

    An arm is *abandoned* when it belongs to a non-final fresh-repetition attempt.
    Completed and abandoned are intentionally independent: the first arm in a
    failed attempt can have completed before another arm triggers a restart.
    """
    summaries: list[OperationalArmSummary] = []
    for arm in Arm:
        attempted = completed = abandoned = model_calls = tool_calls = 0
        elapsed = 0.0
        usage = TokenUsage()
        provider_errors = transport_errors = failed_sql = 0
        for repetition_run in repetition_runs:
            attempts = _sequence(repetition_run, "attempts")
            final_index = len(attempts) - 1
            for index, attempt in enumerate(attempts):
                for arm_run in _sequence(attempt, "arms"):
                    if Arm(_value(arm_run, "arm")) is not arm:
                        continue
                    attempted += 1
                    abandoned += int(index != final_index)
                    completed += int(_value(arm_run, "terminal_outcome") == "completed")
                    elapsed += _float(arm_run, "elapsed_seconds")
                    model_events = _sequence(arm_run, "model_events", "model_calls")
                    tool_events = _sequence(arm_run, "sql_attempts", "tool_calls")
                    model_calls += len(model_events)
                    tool_calls += len(tool_events)
                    usage += _ledger_usage(arm_run, model_events)
                    provider_errors += sum(
                        _value(event, "outcome") == "provider_error" for event in model_events
                    )
                    transport_errors += sum(
                        _value(event, "outcome") == "transport_error" for event in model_events
                    )
                    failed_sql += sum(
                        sql_attempt.outcome is not SqlAttemptOutcome.success
                        for sql_attempt in sql_attempts_from_ledger(tool_events)
                    )
        summaries.append(
            OperationalArmSummary(
                arm=arm,
                attempted_arm_runs=attempted,
                completed_arm_runs=completed,
                abandoned_arm_runs=abandoned,
                total_elapsed_seconds=elapsed,
                model_calls=model_calls,
                tool_calls=tool_calls,
                usage=usage,
                estimated_cost_usd=estimated_cost_usd(usage, manifest.pricing),
                provider_error_calls=provider_errors,
                transport_error_calls=transport_errors,
                failed_sql_attempts=failed_sql,
            )
        )
    return OperationalSummary(arm_summaries=tuple(summaries))


def _empty_operational_summary() -> OperationalSummary:
    return OperationalSummary(
        arm_summaries=tuple(
            OperationalArmSummary(
                arm=arm,
                attempted_arm_runs=0,
                completed_arm_runs=0,
                abandoned_arm_runs=0,
                total_elapsed_seconds=0.0,
                model_calls=0,
                tool_calls=0,
                usage=TokenUsage(),
                estimated_cost_usd=0.0,
                provider_error_calls=0,
                transport_error_calls=0,
                failed_sql_attempts=0,
            )
            for arm in Arm
        )
    )


def build_experiment_report(
    records: Sequence[ArmMetrics],
    *,
    repetition_runs: Sequence[Any] = (),
    manifest: DataAccessManifest | None = None,
) -> DataAccessReport:
    """Produce raw, arm aggregate, and within-repetition contrasts without causal language."""
    metrics = tuple(records)
    duplicates = {(record.repetition_id, record.arm) for record in metrics}
    if len(duplicates) != len(metrics):
        raise ValueError("report cannot contain duplicate arm metrics for one repetition")
    aggregates = tuple(
        _aggregate(arm, tuple(record for record in metrics if record.arm is arm)) for arm in Arm
    )
    contrasts: list[ArmContrast] = []
    by_repetition = {(record.repetition_id, record.arm): record for record in metrics}
    contrast_arms = (
        (Arm.dsx_packet, Arm.full_data),
        (Arm.packet_and_full_data, Arm.dsx_packet),
        (Arm.packet_and_full_data, Arm.full_data),
    )
    for repetition_id in sorted({record.repetition_id for record in metrics}):
        repetition_records = tuple(
            record for record in metrics if record.repetition_id == repetition_id
        )
        repetition_numbers = {record.repetition_number for record in repetition_records}
        if len(repetition_numbers) != 1:
            raise ValueError("repetition records must agree on repetition_number")
        if {record.arm for record in repetition_records} != set(Arm):
            continue
        repetition_number = repetition_numbers.pop()
        for left_arm, right_arm in contrast_arms:
            left = by_repetition[(repetition_id, left_arm)]
            right = by_repetition[(repetition_id, right_arm)]
            contrasts.append(
                ArmContrast(
                    repetition_id=repetition_id,
                    repetition_number=repetition_number,
                    left_arm=left_arm,
                    right_arm=right_arm,
                    elapsed_seconds=left.elapsed_seconds - right.elapsed_seconds,
                    inference_cost_usd=left.inference_cost_usd - right.inference_cost_usd,
                    model_calls=left.model_calls - right.model_calls,
                    tool_calls=left.tool_calls - right.tool_calls,
                    unsupported_claims=left.claims.unsupported - right.claims.unsupported,
                    failed_discovery_attempts=(
                        left.failed_discovery_attempts - right.failed_discovery_attempts
                    ),
                )
            )
    if repetition_runs and manifest is None:
        raise ValueError("manifest is required to summarize all-attempt operational metrics")
    operational = (
        summarize_operational_attempts(repetition_runs, manifest=manifest)
        if manifest is not None
        else _empty_operational_summary()
    )
    return DataAccessReport(
        report_version="data-access-report-v2",
        raw_arm_metrics=metrics,
        arm_aggregates=aggregates,
        arm_contrasts=tuple(contrasts),
        operational=operational,
    )
