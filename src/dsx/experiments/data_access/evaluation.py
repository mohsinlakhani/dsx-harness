"""Decision claims, evidence resolution, and reproducibility for Data Access.

The module deliberately keeps packet content opaque.  Claims name a small predicate
language, while the case oracle is the only component which interprets those
predicates against a frozen dataset.  This lets later packet versions add richer
provenance without changing the experiment's persisted decision contract.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import duckdb
from pydantic import Field, field_validator, model_validator

from .canonical import (
    JsonPointerTraceAdapter,
    PacketTraceAdapter,
    canonical_digest,
    json_value,
)
from .models import (
    Arm,
    DataAccessContract,
    DataAccessManifest,
    OpaquePacket,
    SqlAttempt,
    SqlAttemptOutcome,
)
from .sql_tool import ReadOnlySqlTool


class Metric(StrEnum):
    """Metrics supported by the synthetic classification case oracle."""

    recall_at_5_percent = "recall_at_5_percent"
    precision_at_5_percent = "precision_at_5_percent"
    pr_auc = "pr_auc"
    accuracy = "accuracy"


class EvidenceKind(StrEnum):
    packet_json_pointer = "packet_json_pointer"
    tool_call = "tool_call"


class PacketJsonPointerEvidence(DataAccessContract):
    """Evidence located in the immutable, opaque DSX packet."""

    kind: Literal[EvidenceKind.packet_json_pointer]
    pointer: str

    @model_validator(mode="before")
    @classmethod
    def adapt_local_reference(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "kind" not in value:
            return {"kind": EvidenceKind.packet_json_pointer, **value}
        return value


class ToolCallEvidence(DataAccessContract):
    """Evidence located in a successful, append-only SQL tool result."""

    kind: Literal[EvidenceKind.tool_call]
    evidence_id: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def adapt_local_reference(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "kind" not in value:
            return {"kind": EvidenceKind.tool_call, **value}
        return value


type EvidenceReference = Annotated[
    PacketJsonPointerEvidence | ToolCallEvidence,
    Field(discriminator="kind"),
]


class ClaimStringValue(DataAccessContract):
    kind: Literal["string"]
    value: str

    @property
    def native(self) -> str:
        return self.value


class ClaimIntegerValue(DataAccessContract):
    kind: Literal["integer"]
    value: int

    @property
    def native(self) -> int:
        return self.value


class ClaimNumberValue(DataAccessContract):
    kind: Literal["number"]
    value: float

    @property
    def native(self) -> float:
        return self.value


class ClaimBooleanValue(DataAccessContract):
    kind: Literal["boolean"]
    value: bool

    @property
    def native(self) -> bool:
        return self.value


class ClaimNullValue(DataAccessContract):
    kind: Literal["null"]
    value: None

    @property
    def native(self) -> None:
        return None


type ClaimScalarValue = Annotated[
    ClaimStringValue
    | ClaimIntegerValue
    | ClaimNumberValue
    | ClaimBooleanValue
    | ClaimNullValue,
    Field(discriminator="kind"),
]


class ClaimListValue(DataAccessContract):
    """A flat, explicitly typed list for generic predicate values."""

    kind: Literal["list"]
    items: tuple[ClaimScalarValue, ...]

    @property
    def native(self) -> tuple[str | int | float | bool | None, ...]:
        return tuple(item.native for item in self.items)


type ClaimValue = Annotated[
    ClaimStringValue
    | ClaimIntegerValue
    | ClaimNumberValue
    | ClaimBooleanValue
    | ClaimNullValue
    | ClaimListValue,
    Field(discriminator="kind"),
]


def _claim_value_payload(value: Any) -> Any:
    """Adapt local Python values while the provider schema remains closed and typed."""
    if isinstance(
        value,
        ClaimStringValue
        | ClaimIntegerValue
        | ClaimNumberValue
        | ClaimBooleanValue
        | ClaimNullValue
        | ClaimListValue,
    ):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("kind"), str):
        return value
    if value is None:
        return {"kind": "null", "value": None}
    if isinstance(value, bool):
        return {"kind": "boolean", "value": value}
    if isinstance(value, int):
        return {"kind": "integer", "value": value}
    if isinstance(value, float):
        return {"kind": "number", "value": value}
    if isinstance(value, str):
        return {"kind": "string", "value": value}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        entries = [_claim_value_payload(item) for item in value]
        if any(isinstance(item, Mapping) and item.get("kind") == "list" for item in entries):
            raise ValueError("claim lists may contain only scalar values")
        return {"kind": "list", "items": entries}
    raise ValueError("claim values must be scalar values or flat scalar lists")


class ClaimArgument(DataAccessContract):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    value: ClaimValue

    @property
    def native_value(self) -> Any:
        return self.value.native


class FactualClaim(DataAccessContract):
    """A testable dataset assertion made by a terminal decision."""

    claim_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    statement: str = Field(min_length=1)
    predicate: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    arguments: tuple[ClaimArgument, ...]
    asserted_value: ClaimValue
    evidence: tuple[EvidenceReference, ...]

    @model_validator(mode="before")
    @classmethod
    def adapt_local_value_inputs(cls, value: Any) -> Any:
        """Allow local callers to use Python scalars without widening JSON Schema."""
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        arguments = payload.get("arguments")
        if isinstance(arguments, Mapping):
            payload["arguments"] = [
                {"key": key, "value": _claim_value_payload(argument)}
                for key, argument in arguments.items()
            ]
        if "asserted_value" in payload:
            payload["asserted_value"] = _claim_value_payload(payload["asserted_value"])
        return payload

    @model_validator(mode="after")
    def validate_argument_keys(self) -> FactualClaim:
        keys = {argument.key for argument in self.arguments}
        if len(keys) != len(self.arguments):
            raise ValueError("claim argument keys must be unique")
        return self

    @property
    def argument_values(self) -> dict[str, Any]:
        return {argument.key: argument.native_value for argument in self.arguments}

    @property
    def asserted_native_value(self) -> Any:
        return self.asserted_value.native


class DataAccessDecision(DataAccessContract):
    """Terminal analysis plan common to both Data Access treatment arms.

    The first eight fields intentionally mirror ``pilot.AnalysisDecision``.  The
    Claim arrays are required in the provider schema and may be explicitly empty.
    """

    primary_metric: Metric
    supporting_metrics: tuple[Metric, ...]
    review_budget_fraction: float = Field(ge=0.05, le=0.05)
    split_strategy: str = Field(min_length=1)
    excluded_columns: tuple[str, ...]
    reasoning: str = Field(min_length=1)
    limitations: tuple[str, ...]
    recommendation: str = Field(min_length=1)
    factual_claims: tuple[FactualClaim, ...]
    narrative_claim_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_claim_identifiers(self) -> DataAccessDecision:
        claim_ids = {claim.claim_id for claim in self.factual_claims}
        if len(claim_ids) != len(self.factual_claims):
            raise ValueError("factual claim IDs must be unique")
        unknown = set(self.narrative_claim_ids) - claim_ids
        if unknown:
            raise ValueError("narrative claim IDs must refer to factual claims")
        return self

    @property
    def excludes_row_id(self) -> bool:
        return "row_id" in self.excluded_columns


def provider_decision_schema(name: str = "data_access_decision") -> dict[str, Any]:
    """Return a strict Responses API schema with no optional object properties."""
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(
            "provider schema name must contain letters, digits, underscores, or hyphens"
        )
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": _responses_schema(DataAccessDecision.model_json_schema()),
    }


def _responses_schema(value: Any) -> Any:
    """Convert Pydantic's discriminated ``oneOf`` form to the documented subset."""
    if isinstance(value, Mapping):
        return {
            ("anyOf" if key == "oneOf" else key): _responses_schema(item)
            for key, item in value.items()
            if key != "discriminator"
        }
    if isinstance(value, list):
        return [_responses_schema(item) for item in value]
    return value


class ClaimStatus(StrEnum):
    supported = "supported"
    unsupported = "unsupported"
    contradicted = "contradicted"
    unverifiable = "unverifiable"


class EvidenceResolutionStatus(StrEnum):
    resolved = "resolved"
    invalid = "invalid"


class EvidenceResolution(DataAccessContract):
    reference: EvidenceReference
    status: EvidenceResolutionStatus
    value_digest: str | None = Field(default=None, min_length=64, max_length=64)
    error: str | None = None
    supports_claim: bool = False

    @model_validator(mode="after")
    def validate_resolution(self) -> EvidenceResolution:
        if self.status is EvidenceResolutionStatus.resolved:
            if self.value_digest is None or self.error is not None:
                raise ValueError("resolved evidence requires a digest and no error")
        elif self.error is None:
            raise ValueError("invalid evidence requires an error")
        return self


class ClaimAssessment(DataAccessContract):
    claim: FactualClaim
    status: ClaimStatus
    expected_value: Any | None = None
    evidence: tuple[EvidenceResolution, ...]

    @field_validator("expected_value")
    @classmethod
    def require_json_expected_value(cls, value: Any) -> Any:
        if value is not None:
            json_value(value)
        return value


class DecisionEvaluation(DataAccessContract):
    assessments: tuple[ClaimAssessment, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(item.status is status for item in self.assessments)
            for status in ClaimStatus
        }

    @property
    def invalid_evidence_references(self) -> int:
        return sum(
            resolution.status is EvidenceResolutionStatus.invalid
            for assessment in self.assessments
            for resolution in assessment.evidence
        )

    @property
    def resolved_evidence_references(self) -> int:
        return sum(
            resolution.status is EvidenceResolutionStatus.resolved
            for assessment in self.assessments
            for resolution in assessment.evidence
        )


class OracleResult(DataAccessContract):
    """The expected value for one predicate; ``known=False`` is intentional."""

    known: bool
    value: Any | None = None

    @field_validator("value")
    @classmethod
    def require_json_value(cls, value: Any) -> Any:
        if value is not None:
            json_value(value)
        return value


class CaseOracle(Protocol):
    """Case-specific truth source used for automatic factual-claim evaluation."""

    def resolve(self, connection: duckdb.DuckDBPyConnection, claim: FactualClaim) -> OracleResult:
        """Return the expected value or an explicit unknown predicate result."""


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _single(connection: duckdb.DuckDBPyConnection, sql: str, parameters: Sequence[Any] = ()) -> Any:
    row = connection.execute(sql, list(parameters)).fetchone()
    if row is None:  # pragma: no cover - aggregate queries always produce one row
        raise RuntimeError("oracle query returned no row")
    return json_value(row[0])


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("predicate argument must be a number")
    return float(value)


class SyntheticPilotOracle:
    """Oracle for the current synthetic pilot schema, evaluated from frozen rows."""

    def __init__(self, *, target_column: str) -> None:
        self._target_column = target_column

    def _columns(self, connection: duckdb.DuckDBPyConnection) -> set[str]:
        return {str(row[0]) for row in connection.execute("DESCRIBE dataset").fetchall()}

    def _column(self, connection: duckdb.DuckDBPyConnection, arguments: Mapping[str, Any]) -> str:
        column = arguments.get("column")
        if not isinstance(column, str) or column not in self._columns(connection):
            raise ValueError("predicate requires an existing string column argument")
        return column

    def resolve(self, connection: duckdb.DuckDBPyConnection, claim: FactualClaim) -> OracleResult:
        predicate = claim.predicate
        arguments = claim.argument_values
        rows = int(_single(connection, "SELECT count(*) FROM dataset"))
        if predicate == "row_count":
            return OracleResult(known=True, value=rows)
        if predicate in {"class_count", "class_rate"}:
            label = arguments.get("label")
            if label is None:
                raise ValueError("class predicate requires label")
            count = int(
                _single(
                    connection,
                    "SELECT count(*) FROM dataset WHERE "
                    f"{_quote_identifier(self._target_column)} = ?",
                    (label,),
                )
            )
            return OracleResult(
                known=True,
                value=count if predicate == "class_count" else count / rows,
            )
        if predicate == "majority_baseline":
            max_count = int(
                _single(
                    connection,
                    f"SELECT coalesce(max(n), 0) FROM (SELECT count(*) AS n FROM dataset "
                    f"GROUP BY {_quote_identifier(self._target_column)})",
                )
            )
            return OracleResult(known=True, value=max_count / rows)
        if predicate == "review_count":
            fraction = _number(arguments.get("fraction", 0.05))
            if not 0.0 <= fraction <= 1.0:
                raise ValueError("review count fraction must be between zero and one")
            return OracleResult(known=True, value=round(rows * fraction))
        if predicate in {"missingness", "uniqueness", "likely_id"}:
            column = self._column(connection, arguments)
            quoted = _quote_identifier(column)
            if predicate == "missingness":
                missing = int(
                    _single(connection, f"SELECT count(*) FROM dataset WHERE {quoted} IS NULL")
                )
                return OracleResult(known=True, value=missing / rows)
            distinct = int(_single(connection, f"SELECT count(DISTINCT {quoted}) FROM dataset"))
            uniqueness = distinct / rows
            if predicate == "uniqueness":
                return OracleResult(known=True, value=uniqueness)
            return OracleResult(
                known=True,
                value=column != self._target_column and math.isclose(uniqueness, 1.0),
            )
        if predicate == "recommended_exclusions":
            columns = sorted(self._columns(connection) - {self._target_column})
            exclusions = tuple(
                column
                for column in columns
                if math.isclose(
                    int(
                        _single(
                            connection,
                            f"SELECT count(DISTINCT {_quote_identifier(column)}) FROM dataset",
                        )
                    )
                    / rows,
                    1.0,
                )
            )
            return OracleResult(known=True, value=exclusions)
        return OracleResult(known=False)


def oracle_for_manifest(manifest: DataAccessManifest) -> CaseOracle:
    """Return the registered oracle for the first shipped Data Access case family."""
    # The pilot oracle intentionally works from any frozen dataset with the configured
    # target.  A future registry can switch on case_id/oracle_version here.
    if manifest.case.oracle_version != "v1":
        raise ValueError(f"no Data Access oracle is registered for {manifest.case.oracle_version}")
    return SyntheticPilotOracle(target_column=manifest.case.target_column)


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return bool(json_value(left) == json_value(right))


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _dataset_query(sql: str, *required_fragments: str) -> bool:
    normalized = _normalized_sql(sql)
    return "from dataset" in normalized and all(
        fragment in normalized for fragment in required_fragments
    )


def _columns_and_rows(attempt: SqlAttempt) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    assert attempt.result is not None
    return (
        tuple(column.name.lower() for column in attempt.result.columns),
        attempt.result.rows,
    )


def _sql_value_equal(value: Any, expected: Any) -> bool:
    """Compare canonicalized DuckDB decimal strings to numeric claim values safely."""
    if (
        isinstance(value, str)
        and isinstance(expected, int | float)
        and not isinstance(expected, bool)
    ):
        try:
            return math.isclose(float(value), float(expected), rel_tol=1e-12, abs_tol=1e-12)
        except ValueError:
            return False
    return _values_equal(value, expected)


def _single_scalar(
    columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...], name: str, expected: Any
) -> bool:
    return columns == (name,) and len(rows) == 1 and len(rows[0]) == 1 and _sql_value_equal(
        rows[0][0], expected
    )


def _single_labeled(
    columns: tuple[str, ...],
    rows: tuple[tuple[Any, ...], ...],
    *,
    value_name: str,
    label: Any,
    expected: Any,
) -> bool:
    return (
        columns == ("label", value_name)
        and len(rows) == 1
        and len(rows[0]) == 2
        and _sql_value_equal(rows[0][0], label)
        and _sql_value_equal(rows[0][1], expected)
    )


def _single_column_fact(
    columns: tuple[str, ...],
    rows: tuple[tuple[Any, ...], ...],
    *,
    value_name: str,
    column: Any,
    expected: Any,
) -> bool:
    return (
        columns == ("column", value_name)
        and len(rows) == 1
        and len(rows[0]) == 2
        and _sql_value_equal(rows[0][0], column)
        and _sql_value_equal(rows[0][1], expected)
    )


def _sql_supports_claim(attempt: SqlAttempt, claim: FactualClaim, *, target_column: str) -> bool:
    """Accept only query results whose shape and SQL text prove one shipped fact."""
    if attempt.result is None:
        return False
    columns, rows = _columns_and_rows(attempt)
    value = claim.asserted_native_value
    arguments = claim.argument_values
    predicate = claim.predicate
    if predicate == "row_count":
        return _dataset_query(attempt.sql, "count(") and _single_scalar(
            columns, rows, "row_count", value
        )
    if predicate == "class_count":
        return (
            _dataset_query(attempt.sql, "count(", "where", target_column.lower())
            and _single_labeled(
                columns,
                rows,
                value_name="class_count",
                label=arguments.get("label"),
                expected=value,
            )
        )
    if predicate == "class_rate":
        return (
            _dataset_query(attempt.sql, "count(", "where", target_column.lower())
            and _single_labeled(
                columns,
                rows,
                value_name="class_rate",
                label=arguments.get("label"),
                expected=value,
            )
        )
    if predicate == "majority_baseline":
        return _dataset_query(attempt.sql, "count(", "max(") and _single_scalar(
            columns, rows, "majority_baseline", value
        )
    if predicate == "review_count":
        return _dataset_query(attempt.sql, "count(") and _single_scalar(
            columns, rows, "review_count", value
        )
    if predicate == "missingness":
        return (
            _dataset_query(attempt.sql, "is null", "count(")
            and _single_column_fact(
                columns,
                rows,
                value_name="missingness",
                column=arguments.get("column"),
                expected=value,
            )
        )
    if predicate == "uniqueness":
        return (
            _dataset_query(attempt.sql, "count(distinct")
            and _single_column_fact(
                columns,
                rows,
                value_name="uniqueness",
                column=arguments.get("column"),
                expected=value,
            )
        )
    if predicate == "likely_id":
        return (
            _dataset_query(attempt.sql, "count(distinct")
            and _single_column_fact(
                columns,
                rows,
                value_name="likely_id",
                column=arguments.get("column"),
                expected=value,
            )
        )
    if predicate == "recommended_exclusions":
        return (
            _dataset_query(attempt.sql, "count(distinct")
            and columns == ("excluded_column",)
            and _values_equal(tuple(row[0] for row in rows if len(row) == 1), value)
            and all(len(row) == 1 for row in rows)
        )
    return False


class V1PacketTraceAdapter(JsonPointerTraceAdapter):
    """Artifact-only v1 tracing; future adapters may override computation replay."""

    def replay_computation(self, packet_payload: Any, reference: str) -> Literal["not_applicable"]:
        del packet_payload, reference
        return "not_applicable"


def _resolve_evidence(
    reference: EvidenceReference,
    *,
    packet: OpaquePacket,
    sql_results: Mapping[str, SqlAttempt],
    claim: FactualClaim,
    target_column: str,
    packet_trace_adapter: PacketTraceAdapter,
    allowed_evidence_kinds: frozenset[EvidenceKind] = frozenset(EvidenceKind),
) -> EvidenceResolution:
    try:
        if reference.kind not in allowed_evidence_kinds:
            raise ValueError("evidence kind is unavailable in this experiment arm")
        if isinstance(reference, PacketJsonPointerEvidence):
            value = packet_trace_adapter.resolve(packet.value, reference.pointer)
            supports_claim = _values_equal(value, claim.asserted_native_value)
        else:
            attempt = sql_results.get(reference.evidence_id)
            if attempt is None or attempt.result is None:
                raise ValueError("tool evidence ID does not identify a successful SQL result")
            value = {"columns": attempt.result.columns, "rows": attempt.result.rows}
            supports_claim = _sql_supports_claim(
                attempt, claim, target_column=target_column
            )
        return EvidenceResolution(
            reference=reference,
            status=EvidenceResolutionStatus.resolved,
            value_digest=canonical_digest(value),
            supports_claim=supports_claim,
        )
    except (TypeError, ValueError) as error:
        return EvidenceResolution(
            reference=reference,
            status=EvidenceResolutionStatus.invalid,
            error=str(error),
        )


def sql_attempts_from_ledger(entries: Sequence[Any]) -> tuple[SqlAttempt, ...]:
    """Adapt direct attempts or execution ``ToolCallLedger`` objects without a cycle."""
    attempts: list[SqlAttempt] = []
    for entry in entries:
        attempt = entry if isinstance(entry, SqlAttempt) else getattr(entry, "sql_attempt", None)
        if not isinstance(attempt, SqlAttempt):
            raise ValueError("SQL evidence ledger must contain SqlAttempt or ToolCallLedger")
        attempts.append(attempt)
    return tuple(attempts)


def evaluate_decision(
    decision: DataAccessDecision,
    manifest: DataAccessManifest,
    sql_attempts: Sequence[Any] = (),
    *,
    arm: Arm,
    oracle: CaseOracle | None = None,
    packet_trace_adapter: PacketTraceAdapter | None = None,
) -> DecisionEvaluation:
    """Classify each claim against frozen truth and its cited immutable evidence."""
    allowed_evidence_kinds = {
        Arm.dsx_packet: frozenset({EvidenceKind.packet_json_pointer}),
        Arm.full_data: frozenset({EvidenceKind.tool_call}),
        Arm.packet_and_full_data: frozenset(EvidenceKind),
    }[arm]
    normalized_attempts = sql_attempts_from_ledger(sql_attempts)
    results = {
        attempt.result.evidence_id: attempt
        for attempt in normalized_attempts
        if attempt.outcome is SqlAttemptOutcome.success and attempt.result is not None
    }
    active_oracle = oracle or oracle_for_manifest(manifest)
    trace_adapter = packet_trace_adapter or V1PacketTraceAdapter()
    connection = duckdb.connect(manifest.dataset.database_path, read_only=True)
    try:
        assessments: list[ClaimAssessment] = []
        for claim in decision.factual_claims:
            evidence = tuple(
                _resolve_evidence(
                    reference,
                    packet=manifest.packet,
                    sql_results=results,
                    claim=claim,
                    target_column=manifest.case.target_column,
                    packet_trace_adapter=trace_adapter,
                    allowed_evidence_kinds=allowed_evidence_kinds,
                )
                for reference in claim.evidence
            )
            try:
                truth = active_oracle.resolve(connection, claim)
            except (duckdb.Error, ValueError):
                # Invalid case-specific predicate arguments are unverifiable, not an
                # opportunity to treat a malformed assertion as an evidence failure.
                truth = OracleResult(known=False)
            if not truth.known:
                status = ClaimStatus.unverifiable
            elif not _values_equal(claim.asserted_native_value, truth.value):
                status = ClaimStatus.contradicted
            elif any(item.supports_claim for item in evidence):
                status = ClaimStatus.supported
            else:
                status = ClaimStatus.unsupported
            assessments.append(
                ClaimAssessment(
                    claim=claim,
                    status=status,
                    expected_value=truth.value if truth.known else None,
                    evidence=evidence,
                )
            )
        return DecisionEvaluation(assessments=tuple(assessments))
    finally:
        connection.close()


class SqlReplayStatus(StrEnum):
    matched = "matched"
    mismatched = "mismatched"
    replay_failed = "replay_failed"


class SqlReplay(DataAccessContract):
    evidence_id: str = Field(min_length=1)
    status: SqlReplayStatus
    original_digest: str = Field(min_length=64, max_length=64)
    replay_digest: str | None = Field(default=None, min_length=64, max_length=64)
    error: str | None = None


def replay_sql_evidence(
    manifest: DataAccessManifest,
    sql_attempts: Sequence[Any],
    cited_evidence_ids: Sequence[str],
) -> tuple[SqlReplay, ...]:
    """Re-run cited successful queries against the committed database artifact."""
    normalized_attempts = sql_attempts_from_ledger(sql_attempts)
    successful = {
        attempt.result.evidence_id: attempt
        for attempt in normalized_attempts
        if attempt.outcome is SqlAttemptOutcome.success and attempt.result is not None
    }
    tool = ReadOnlySqlTool(
        Path(manifest.dataset.database_path),
        timeout_seconds=manifest.limits.sql_timeout_seconds,
        max_rows=manifest.limits.sql_max_rows,
        max_result_bytes=manifest.limits.sql_max_result_bytes,
    )
    records: list[SqlReplay] = []
    for evidence_id in dict.fromkeys(cited_evidence_ids):
        source = successful.get(evidence_id)
        if source is None or source.result is None:
            continue
        replay = tool.execute(source.sql)
        if replay.result is None:
            records.append(
                SqlReplay(
                    evidence_id=evidence_id,
                    status=SqlReplayStatus.replay_failed,
                    original_digest=source.result.result_digest,
                    error=replay.error_message or replay.outcome.value,
                )
            )
            continue
        records.append(
            SqlReplay(
                evidence_id=evidence_id,
                status=(
                    SqlReplayStatus.matched
                    if replay.result.result_digest == source.result.result_digest
                    else SqlReplayStatus.mismatched
                ),
                original_digest=source.result.result_digest,
                replay_digest=replay.result.result_digest,
            )
        )
    return tuple(records)


def cited_tool_evidence_ids(decision: DataAccessDecision) -> tuple[str, ...]:
    """Return cited SQL evidence IDs in stable first-occurrence order."""
    return tuple(
        dict.fromkeys(
            reference.evidence_id
            for claim in decision.factual_claims
            for reference in claim.evidence
            if isinstance(reference, ToolCallEvidence)
        )
    )
