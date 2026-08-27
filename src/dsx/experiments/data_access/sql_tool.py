"""Safe, auditable, read-only DuckDB discovery access for the full-data arm."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

from .canonical import canonical_byte_count, canonical_digest, json_value
from .models import SqlAttempt, SqlAttemptOutcome, SqlColumn, SqlResult

QUERY_DATA_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "query_data",
    "description": "Run one read-only SQL query against the full dataset table named dataset.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"sql": {"type": "string", "minLength": 1}},
        "required": ["sql"],
    },
}

_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "http_get",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "glob",
        "query_table",
        "current_setting",
        "getvariable",
        "setvariable",
        "current_database",
        "current_schema",
        "current_user",
        "version",
    }
)
_DESCRIBE_DATASET = re.compile(r"^DESCRIBE\s+(?:TABLE\s+)?dataset$", re.IGNORECASE)
_SQL_TOKEN = re.compile(
    r"(?P<line_comment>--[^\n]*)|"
    r"(?P<block_comment>/\*.*?\*/)|"
    r"(?P<identifier>\"(?:[^\"]|\"\")*\")|"
    r"(?P<string>'(?:[^']|'')*')|"
    r"(?P<word>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?P<punct>[(),.])",
    re.DOTALL,
)


def _tokens(statement: str) -> tuple[tuple[str, str], ...]:
    """Tokenize only the SQL fragments relevant to the narrow policy.

    String contents are deliberately discarded; quoted identifiers are retained as
    identifiers. This lets a dataset legitimately expose columns named ``load`` or
    ``version`` without allowing those words to become operations or function calls.
    """
    return tuple(
        (
            match.lastgroup or "",
            ""
            if match.lastgroup in {"string", "line_comment", "block_comment"}
            else match.group().removeprefix('"').removesuffix('"').replace('""', '"').lower(),
        )
        for match in _SQL_TOKEN.finditer(statement)
    )


def _is_forbidden_function(name: str) -> bool:
    return (
        name in _FORBIDDEN_FUNCTIONS
        or name.startswith("duckdb_")
        or name.startswith("pragma_")
    )


def _references_dataset(tokens: tuple[tuple[str, str], ...]) -> bool:
    return any(
        name == "dataset"
        and kind in {"word", "identifier"}
        and index > 0
        and tokens[index - 1][1] in {"from", "join"}
        for index, (kind, name) in enumerate(tokens)
    )


def _has_forbidden_function(tokens: tuple[tuple[str, str], ...]) -> bool:
    return any(
        kind in {"word", "identifier"}
        and _is_forbidden_function(name)
        and tokens[index + 1] == ("punct", "(")
        for index, (kind, name) in enumerate(tokens[:-1])
    )


def validate_sql(sql: str) -> str:
    """Validate the deliberately narrow one-statement discovery SQL language."""
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL argument must be a non-empty string")
    statement = sql.strip()
    try:
        statements = duckdb.extract_statements(statement)
    except duckdb.ParserException as error:
        raise PermissionError("SQL must parse as one read-only discovery statement") from error
    if len(statements) != 1:
        raise PermissionError("only one SQL statement is permitted")
    parsed = statements[0]
    statement = parsed.query.strip().removesuffix(";").rstrip()
    tokens = _tokens(statement)
    if parsed.type != duckdb.StatementType.SELECT:
        raise PermissionError("SQL contains a forbidden operation")
    if _has_forbidden_function(tokens):
        raise PermissionError("SQL contains a forbidden external, metadata, or settings function")
    upper = statement.upper()
    if _DESCRIBE_DATASET.fullmatch(statement):
        return statement
    if upper.startswith("SELECT ") or upper.startswith("WITH "):
        if not _references_dataset(tokens):
            raise PermissionError("discovery queries must reference the dataset relation")
        return statement
    raise PermissionError("only SELECT, WITH, or DESCRIBE dataset statements are permitted")


def _attempt_id(sql: str, ordinal: int | None) -> str:
    identity = f"{ordinal if ordinal is not None else 0}:{sql}".encode()
    return "sql-" + hashlib.sha256(identity).hexdigest()[:24]


class ReadOnlySqlTool:
    """Execute bounded full-data discovery queries and produce immutable ledgers."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        timeout_seconds: float = 10.0,
        max_rows: int = 1000,
        max_result_bytes: int = 64 * 1024,
    ) -> None:
        if timeout_seconds <= 0 or max_rows <= 0 or max_result_bytes <= 0:
            raise ValueError("SQL limits must be positive")
        self._database_path = str(database_path)
        self._timeout_seconds = timeout_seconds
        self._max_rows = max_rows
        self._max_result_bytes = max_result_bytes

    def execute(self, sql: object, *, ordinal: int | None = None) -> SqlAttempt:
        """Run an allowed query; failure is a typed observable discovery outcome."""
        raw_sql = sql if isinstance(sql, str) else "<non-string SQL argument>"
        attempt_id = _attempt_id(raw_sql, ordinal)
        started = time.perf_counter()
        if not isinstance(sql, str) or not sql.strip():
            return SqlAttempt(
                attempt_id=attempt_id,
                sql=raw_sql,
                outcome=SqlAttemptOutcome.invalid_argument,
                elapsed_seconds=time.perf_counter() - started,
                error_message="SQL argument must be a non-empty string",
            )
        try:
            statement = validate_sql(sql)
        except PermissionError as error:
            return SqlAttempt(
                attempt_id=attempt_id,
                sql=sql,
                outcome=SqlAttemptOutcome.policy_rejected,
                elapsed_seconds=time.perf_counter() - started,
                error_message=str(error),
            )
        except ValueError as error:
            return SqlAttempt(
                attempt_id=attempt_id,
                sql=sql,
                outcome=SqlAttemptOutcome.invalid_argument,
                elapsed_seconds=time.perf_counter() - started,
                error_message=str(error),
            )

        connection: duckdb.DuckDBPyConnection | None = None
        timer: threading.Timer | None = None
        timed_out = False

        def interrupt() -> None:
            nonlocal timed_out
            timed_out = True
            # The timer is created only after assigning the connection below.
            assert connection is not None
            connection.interrupt()

        try:
            connection = duckdb.connect(
                self._database_path,
                read_only=True,
                config={"enable_external_access": "false"},
            )
            timer = threading.Timer(self._timeout_seconds, interrupt)
            timer.daemon = True
            timer.start()
            cursor = connection.execute(statement)
            rows = cursor.fetchmany(self._max_rows + 1)
            if timed_out:
                raise TimeoutError("SQL execution exceeded timeout")
            elapsed = time.perf_counter() - started
            if len(rows) > self._max_rows:
                return SqlAttempt(
                    attempt_id=attempt_id,
                    sql=sql,
                    outcome=SqlAttemptOutcome.result_too_large,
                    elapsed_seconds=elapsed,
                    error_message=(
                        f"query returned more than {self._max_rows} rows; aggregate or LIMIT it"
                    ),
                )
            description = cursor.description or []
            columns = tuple(
                SqlColumn(name=str(item[0]), type_name=str(item[1])) for item in description
            )
            json_rows = tuple(tuple(json_value(item) for item in row) for row in rows)
            payload = {"columns": columns, "rows": json_rows}
            byte_count = canonical_byte_count(payload)
            if byte_count > self._max_result_bytes:
                return SqlAttempt(
                    attempt_id=attempt_id,
                    sql=sql,
                    outcome=SqlAttemptOutcome.result_too_large,
                    elapsed_seconds=elapsed,
                    error_message=(
                        f"query result is {byte_count} bytes; limit is {self._max_result_bytes}; "
                        "aggregate or select fewer columns"
                    ),
                )
            digest = canonical_digest(payload)
            evidence_id = "sql-" + hashlib.sha256(f"{statement}:{digest}".encode()).hexdigest()[:32]
            return SqlAttempt(
                attempt_id=attempt_id,
                sql=sql,
                outcome=SqlAttemptOutcome.success,
                elapsed_seconds=elapsed,
                result=SqlResult(
                    evidence_id=evidence_id,
                    columns=columns,
                    rows=json_rows,
                    result_digest=digest,
                    byte_count=byte_count,
                ),
            )
        except TimeoutError as error:
            return SqlAttempt(
                attempt_id=attempt_id,
                sql=sql,
                outcome=SqlAttemptOutcome.timeout,
                elapsed_seconds=time.perf_counter() - started,
                error_message=str(error),
            )
        except duckdb.Error as error:
            category = SqlAttemptOutcome.timeout if timed_out else SqlAttemptOutcome.execution_error
            return SqlAttempt(
                attempt_id=attempt_id,
                sql=sql,
                outcome=category,
                elapsed_seconds=time.perf_counter() - started,
                error_message=str(error),
            )
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()
