from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dsx.experiments.data_access import sql_tool
from dsx.experiments.data_access.models import (
    CaseConfig,
    DatasetFormat,
    ModelConfig,
    OpaquePacket,
    PricingSnapshot,
    SqlAttemptOutcome,
    TokenPrice,
)
from dsx.experiments.data_access.prepare import prepare_manifest
from dsx.experiments.data_access.sql_tool import ReadOnlySqlTool, validate_sql


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "dataset.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE dataset(id INTEGER, label INTEGER, value VARCHAR)")
    connection.execute("INSERT INTO dataset VALUES (1, 0, 'one'), (2, 1, 'two'), (3, 0, 'three')")
    connection.close()
    return path


@pytest.fixture
def prepared_keyword_database(tmp_path: Path) -> Path:
    source = tmp_path / "keyword-columns.csv"
    source.write_text(
        "version,load,copy,attach,label\nv1,loader,copy-value,attached,1\n",
        encoding="utf-8",
    )
    manifest = prepare_manifest(
        case=CaseConfig(
            case_id="keyword-columns",
            task_prompt="Inspect the prepared dataset.",
            dataset_path=str(source),
            dataset_format=DatasetFormat.csv,
            target_column="label",
        ),
        packet=OpaquePacket.from_value({"source": "test"}),
        model=ModelConfig(model_identifier="test", system_prompt="Return JSON."),
        pricing=PricingSnapshot(
            input=TokenPrice(usd_per_million_tokens=1),
            output=TokenPrice(usd_per_million_tokens=1),
            source="test",
            effective_date="2026-08-26",
        ),
        database_path=tmp_path / "prepared.duckdb",
    )
    return Path(manifest.dataset.database_path)


def test_read_only_tool_returns_committed_evidence_and_describe(database: Path) -> None:
    tool = ReadOnlySqlTool(database)
    first = tool.execute("SELECT label, count(*) AS n FROM dataset GROUP BY label", ordinal=1)
    second = tool.execute("SELECT label, count(*) AS n FROM dataset GROUP BY label", ordinal=2)
    assert first.outcome is SqlAttemptOutcome.success
    assert first.result is not None
    assert first.result.evidence_id == second.result.evidence_id if second.result else False
    assert first.attempt_id != second.attempt_id
    schema = tool.execute("DESCRIBE dataset")
    assert schema.outcome is SqlAttemptOutcome.success
    assert schema.result is not None and schema.result.rows[0][0] == "id"


def test_prepared_dataset_keeps_keyword_named_columns_queryable(
    prepared_keyword_database: Path,
) -> None:
    tool = ReadOnlySqlTool(prepared_keyword_database)
    unquoted = tool.execute("SELECT version, load, copy, attach FROM dataset")
    quoted = tool.execute('SELECT "version", "load", "copy", "attach" FROM "dataset"')
    literal = tool.execute("SELECT 'current_setting()' AS note, version FROM dataset")
    assert unquoted.outcome is SqlAttemptOutcome.success
    assert quoted.outcome is SqlAttemptOutcome.success
    assert literal.outcome is SqlAttemptOutcome.success
    assert unquoted.result is not None
    assert unquoted.result.rows == (("v1", "loader", "copy-value", "attached"),)


@pytest.mark.parametrize(
    ("sql", "outcome"),
    [
        ("", SqlAttemptOutcome.invalid_argument),
        ("DELETE FROM dataset", SqlAttemptOutcome.policy_rejected),
        ("WITH x AS (SELECT 1) DELETE FROM dataset", SqlAttemptOutcome.policy_rejected),
        ("SELECT * FROM read_csv_auto('/tmp/nope.csv')", SqlAttemptOutcome.policy_rejected),
        ("SELECT * FROM duckdb_tables()", SqlAttemptOutcome.policy_rejected),
        (
            "SELECT current_setting('home_directory') FROM dataset",
            SqlAttemptOutcome.policy_rejected,
        ),
        ("SELECT getvariable('duckdb_api') FROM dataset", SqlAttemptOutcome.policy_rejected),
        ("SELECT current_database() FROM dataset", SqlAttemptOutcome.policy_rejected),
        ("SELECT version() FROM dataset", SqlAttemptOutcome.policy_rejected),
        ("SELECT 1", SqlAttemptOutcome.policy_rejected),
        ("SELECT 1; SELECT 2", SqlAttemptOutcome.policy_rejected),
        ("SELECT missing FROM dataset", SqlAttemptOutcome.execution_error),
    ],
)
def test_tool_records_typed_discovery_failures(
    database: Path, sql: str, outcome: SqlAttemptOutcome
) -> None:
    assert ReadOnlySqlTool(database).execute(sql).outcome is outcome


def test_tool_enforces_row_and_byte_limits(database: Path) -> None:
    assert (
        ReadOnlySqlTool(database, max_rows=2).execute("SELECT * FROM dataset").outcome
        is SqlAttemptOutcome.result_too_large
    )
    assert (
        ReadOnlySqlTool(database, max_result_bytes=20).execute("SELECT * FROM dataset").outcome
        is SqlAttemptOutcome.result_too_large
    )


def test_sql_validator_only_allows_single_read_statement() -> None:
    assert validate_sql(" SELECT * FROM dataset; ") == "SELECT * FROM dataset"
    assert validate_sql("WITH x AS (SELECT 1) SELECT * FROM dataset").startswith("WITH")
    assert validate_sql('SELECT * FROM "dataset"').startswith("SELECT")
    assert validate_sql("SELECT '; -- /* */' AS note FROM dataset").startswith("SELECT")
    assert validate_sql("SELECT /* ordinary comment */ * FROM dataset").startswith("SELECT")
    assert validate_sql("SELECT * FROM dataset -- ; DELETE FROM dataset").startswith("SELECT")
    with pytest.raises(PermissionError):
        validate_sql("DESCRIBE other")
    with pytest.raises(PermissionError, match="dataset relation"):
        validate_sql("WITH x AS (SELECT 1) SELECT * FROM x")
    with pytest.raises(PermissionError, match="one SQL statement"):
        validate_sql("SELECT * FROM dataset; SELECT * FROM dataset")
    with pytest.raises(PermissionError, match="one SQL statement"):
        validate_sql("SELECT * FROM dataset -- comment\n; DELETE FROM dataset")
    with pytest.raises(PermissionError, match="forbidden operation"):
        validate_sql("WITH x AS (SELECT 1) DELETE FROM dataset")
    with pytest.raises(PermissionError, match="parse"):
        validate_sql("SELECT FROM dataset")
    with pytest.raises(ValueError):
        validate_sql(None)  # type: ignore[arg-type]


def test_tool_executes_parser_accepted_literals_and_comments(database: Path) -> None:
    tool = ReadOnlySqlTool(database)
    literal = tool.execute("SELECT '; -- /* */' AS note FROM dataset")
    comment = tool.execute("SELECT value FROM dataset -- ; DELETE FROM dataset")
    assert literal.outcome is SqlAttemptOutcome.success
    assert comment.outcome is SqlAttemptOutcome.success
    assert literal.result is not None and literal.result.rows[0] == ("; -- /* */",)


def test_constructor_and_runtime_timeout_value_and_connection_error_paths(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="limits"):
        ReadOnlySqlTool(database, timeout_seconds=0)
    with pytest.raises(ValueError, match="limits"):
        ReadOnlySqlTool(database, max_rows=0)
    with pytest.raises(ValueError, match="limits"):
        ReadOnlySqlTool(database, max_result_bytes=0)
    assert ReadOnlySqlTool(database).execute(None).outcome is SqlAttemptOutcome.invalid_argument
    original_validate = sql_tool.validate_sql
    monkeypatch.setattr(
        sql_tool, "validate_sql", lambda _sql: (_ for _ in ()).throw(ValueError("bad"))
    )
    assert (
        ReadOnlySqlTool(database).execute("SELECT 1").outcome
        is SqlAttemptOutcome.invalid_argument
    )
    monkeypatch.setattr(sql_tool, "validate_sql", original_validate)
    assert (
        ReadOnlySqlTool(database.parent / "missing.duckdb").execute("SELECT * FROM dataset").outcome
        is SqlAttemptOutcome.execution_error
    )


def test_tool_records_timeout_after_timer_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    active_timer: list[object] = []

    class Cursor:
        description: list[object] = []

        def fetchmany(self, _size: int) -> list[tuple[object, ...]]:
            timer = active_timer[0]
            assert isinstance(timer, Timer)
            timer.callback()
            return []

    class Connection:
        def execute(self, _sql: str) -> Cursor:
            return Cursor()

        def interrupt(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Timer:
        def __init__(self, _seconds: float, callback: object) -> None:
            self.callback = callback
            self.daemon = False
            active_timer.append(self)

        def start(self) -> None:
            return None

        def cancel(self) -> None:
            return None

    monkeypatch.setattr(sql_tool.duckdb, "connect", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(sql_tool.threading, "Timer", Timer)
    outcome = ReadOnlySqlTool("unused").execute("SELECT * FROM dataset")
    assert outcome.outcome is SqlAttemptOutcome.timeout
