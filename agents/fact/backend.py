"""Deterministic tool backend for the FACT agent directory.

Upstream: https://github.com/ruvnet/FACT @ b0e343583cd8f64549dbcba4b7c709e48b3a6a08 (MIT).
See ATTRIBUTION.md for every delta from the real runtime and ADAPT_EDITS.md for what was
changed relative to the `upshift adapt` output this file replaces.

The three tools mirror `src/tools/connectors/sql.py`:

  SQL_QueryReadonly   <- sql_query_readonly (sql.py:135-181) via SQLQueryTool.execute_query
                         (sql.py:50-117) and DatabaseManager.execute_query /
                         validate_sql_query (src/db/connection.py:321-551)
  SQL_GetSchema       <- sql_get_schema (sql.py:184-273)
  SQL_GetSampleQueries<- sql_get_sample_queries (sql.py:276-330), list copied verbatim

Determinism (ADAPTER.md requirement 3): the SQLite file is opened READ-ONLY
(`file:...?mode=ro`), never written, and every value upstream derives from the clock
(`query_id`, `execution_time_ms`) is replaced by a counter / constant here. Given the same
`initial_state` and the same sequence of `execute` calls the backend returns the same results
and the same final state.
"""

from __future__ import annotations

import copy
import re
import sqlite3
from pathlib import Path
from typing import Any

#: The database committed next to this file, copied verbatim from upstream `data/fact_demo.db`
#: (81,920 bytes, md5 a0ff57cf5c8474083321514146bbade9). `src/core/config.py:121` makes
#: `data/fact_demo.db` the default `DATABASE_PATH`.
DB_PATH = Path(__file__).resolve().parent / "fact_demo.db"

#: Upstream `DatabaseManager.validate_sql_query` (src/db/connection.py:344-349).
_DANGEROUS_KEYWORDS = (
    "drop", "delete", "update", "insert", "alter", "create", "truncate", "replace",
    "merge", "exec", "execute", "attach", "detach", "vacuum", "reindex", "analyze",
)

#: Upstream injection patterns (src/db/connection.py:388-397), applied to non-PRAGMA queries.
_INJECTION_PATTERNS = (
    r"--",
    r"/\*.*?\*/",
    r";\s*\w+",
    r"\bunion\s+select\b",
    r"\bor\s+1\s*=\s*1\b",
    r"\band\s+1\s*=\s*1\b",
    r"\bor\s+\'.*?\'\s*=\s*\'.*?\'",
    r"\'.*?\'\s*or\s*\'.*?\'",
    r"\\x[0-9a-f]{2}",
)

#: `sample_queries` from src/tools/connectors/sql.py:291-323, copied verbatim.
SAMPLE_QUERIES: list[dict[str, str]] = [
    {
        "description": "Get all companies in the Technology sector",
        "query": "SELECT * FROM companies WHERE sector = 'Technology'",
    },
    {
        "description": "Get total revenue by company for 2024",
        "query": (
            "SELECT c.name, SUM(f.revenue) as total_revenue FROM companies c JOIN "
            "financial_records f ON c.id = f.company_id WHERE f.year = 2024 GROUP BY c.id, "
            "c.name ORDER BY total_revenue DESC"
        ),
    },
    {
        "description": "Get Q1 2025 financial results",
        "query": (
            "SELECT c.name, f.revenue, f.profit, f.expenses FROM companies c JOIN "
            "financial_records f ON c.id = f.company_id WHERE f.quarter = 'Q1' AND f.year = "
            "2025 ORDER BY f.revenue DESC"
        ),
    },
    {
        "description": "Get company count by sector",
        "query": (
            "SELECT sector, COUNT(*) as company_count FROM companies GROUP BY sector ORDER BY "
            "company_count DESC"
        ),
    },
    {
        "description": "Get TechCorp's quarterly performance over time",
        "query": (
            "SELECT c.name, f.quarter, f.year, f.revenue, f.profit FROM companies c JOIN "
            "financial_records f ON c.id = f.company_id WHERE c.symbol = 'TECH' ORDER BY "
            "f.year DESC, f.quarter DESC"
        ),
    },
    {
        "description": "Get average metrics for 2024",
        "query": (
            "SELECT AVG(revenue) as avg_revenue, AVG(profit) as avg_profit, AVG(expenses) as "
            "avg_expenses FROM financial_records WHERE year = 2024"
        ),
    },
    {
        "description": "Get top companies by market cap with latest revenue",
        "query": (
            "SELECT c.name, c.market_cap, f.revenue as q1_2025_revenue FROM companies c JOIN "
            "financial_records f ON c.id = f.company_id WHERE f.year = 2025 AND f.quarter = "
            "'Q1' ORDER BY c.market_cap DESC"
        ),
    },
]


class SecurityError(Exception):
    """Mirror of `src/core/errors.py`'s SecurityError; raised by :func:`validate_sql_query`."""


class InvalidSQLError(Exception):
    """Mirror of `src/core/errors.py`'s InvalidSQLError."""


def validate_sql_query(statement: str) -> None:
    """Upstream `DatabaseManager.validate_sql_query` (src/db/connection.py:321-431).

    The validation-result cache is dropped (it only affects timing) and syntax validation runs
    `EXPLAIN QUERY PLAN` on the read-only connection instead of a fresh writable one.
    """
    normalized = statement.lower().strip()
    is_select = normalized.startswith("select")
    is_safe_pragma = normalized.startswith("pragma table_info")
    if not (is_select or is_safe_pragma):
        raise SecurityError("Only SELECT statements and PRAGMA table_info queries are allowed")
    if normalized.startswith("pragma") and not is_safe_pragma:
        raise SecurityError("Only PRAGMA table_info queries are allowed")
    for keyword in _DANGEROUS_KEYWORDS:
        if re.search(r"\b" + re.escape(keyword) + r"\b", normalized, re.IGNORECASE):
            raise SecurityError(f"Dangerous SQL keyword detected: {keyword}")
    if not is_safe_pragma:
        for pattern in _INJECTION_PATTERNS:
            if re.search(pattern, normalized, re.IGNORECASE):
                raise SecurityError(
                    f"Potential SQL injection pattern detected: {pattern} in query: "
                    f"{normalized[:100]}"
                )
    if len(statement) > 5000:
        raise SecurityError("Query too long - potential DoS attack")
    if normalized.count("select") > 5:
        raise SecurityError("Too many nested subqueries - potential injection attack")


def _is_valid_table_name(table_name: str) -> bool:
    """Upstream `DatabaseManager._is_valid_table_name` (src/db/connection.py:438-472)."""
    if not table_name or len(table_name) > 64:
        return False
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        return False
    forbidden = {"sqlite_master", "sqlite_sequence", "sqlite_temp_master", "sqlite_stat1"}
    return table_name.lower() not in forbidden


def _open_readonly() -> sqlite3.Connection:
    """Open the committed database strictly read-only.

    `mode=ro` makes every write a `sqlite3.OperationalError` at the SQLite layer, so the
    read-only guarantee does not depend on :func:`validate_sql_query` alone.
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class Backend:
    """One episode's state. Construct via :func:`create_backend`."""

    def __init__(self, initial_state: dict[str, Any] | None = None) -> None:
        state = copy.deepcopy(initial_state or {})
        self._initial: dict[str, Any] = state if isinstance(state, dict) else {}
        self._queries_run = 0
        self._statements: list[str] = []
        self._rejected: list[str] = []
        self._last_rows: list[dict[str, Any]] = []
        self._last_row_count = 0
        self._last_columns: list[str] = []
        self._schema_calls = 0
        self._sample_query_calls = 0

    # -- ADAPTER.md contract ------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call. Never raises (ADAPTER.md requirement 2)."""
        try:
            if not isinstance(arguments, dict):
                return {"error": f"arguments must be an object, got {type(arguments).__name__}"}
            if name == "SQL_QueryReadonly":
                return self._sql_query_readonly(arguments)
            if name == "SQL_GetSchema":
                return self._sql_get_schema()
            if name == "SQL_GetSampleQueries":
                return self._sql_get_sample_queries()
            return {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001 - defensive: execute never raises
            return {"error": f"backend failure in {name}: {exc}"}

    def state(self) -> dict[str, Any]:
        """Deterministic summary of what the tools did this episode."""
        out = copy.deepcopy(self._initial)
        out.update(
            {
                "queries_run": self._queries_run,
                "statements": list(self._statements),
                "rejected_statements": list(self._rejected),
                "schema_calls": self._schema_calls,
                "sample_query_calls": self._sample_query_calls,
                "last_row_count": self._last_row_count,
                "last_columns": list(self._last_columns),
                "last_rows": copy.deepcopy(self._last_rows),
            }
        )
        return out

    # -- tools --------------------------------------------------------------

    def _query_id(self) -> str:
        """Upstream builds `query_{int(time.time()*1000)}` (sql.py:65); a per-episode counter
        is the deterministic stand-in."""
        return f"query_{self._queries_run}"

    def _run_query(self, statement: str) -> tuple[list[dict[str, Any]], list[str]]:
        conn = _open_readonly()
        try:
            rows = conn.execute(statement).fetchall()
        finally:
            conn.close()
        if not rows:
            return [], []
        columns = list(rows[0].keys())
        return [{col: row[col] for col in columns} for row in rows], columns

    def _sql_query_readonly(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """`sql_query_readonly` (sql.py:150) -> `SQLQueryTool.execute_query` (sql.py:50)."""
        statement = arguments.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            # Upstream would raise a TypeError inside the registry; the contract here is an
            # error result the model can read.
            return {"error": "missing required argument: statement", "status": "failed"}
        self._queries_run += 1
        query_id = self._query_id()
        try:
            validate_sql_query(statement)
            rows, columns = self._run_query(statement)
        except (SecurityError, InvalidSQLError) as exc:
            self._rejected.append(statement)
            return {
                "query_id": query_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "execution_time_ms": 0.0,
                "statement": self._truncate(statement),
                "status": "failed",
            }
        except sqlite3.Error as exc:
            # Upstream wraps execution failures as DatabaseError (src/db/connection.py:551).
            self._rejected.append(statement)
            return {
                "query_id": query_id,
                "error": f"Query execution failed: {exc}",
                "error_type": "DatabaseError",
                "execution_time_ms": 0.0,
                "statement": self._truncate(statement),
                "status": "failed",
            }
        self._statements.append(statement)
        self._last_rows = rows
        self._last_row_count = len(rows)
        self._last_columns = columns
        return {
            "query_id": query_id,
            "rows": rows,
            "row_count": len(rows),
            "columns": columns,
            "execution_time_ms": 0.0,
            "statement": self._truncate(statement),
            "status": "success",
        }

    def _sql_get_schema(self) -> dict[str, Any]:
        """`sql_get_schema` (sql.py:192-273): sqlite_master + PRAGMA table_info per table."""
        self._schema_calls += 1
        conn = _open_readonly()
        try:
            table_rows = conn.execute(
                "SELECT name as table_name FROM sqlite_master WHERE type='table' AND name NOT "
                "LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables: list[dict[str, Any]] = []
            for table_row in table_rows:
                table_name = table_row["table_name"]
                if not _is_valid_table_name(table_name):
                    continue
                columns = [
                    {
                        "name": col["name"],
                        "type": col["type"],
                        "nullable": not col["notnull"],
                        "primary_key": bool(col["pk"]),
                    }
                    for col in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                ]
                tables.append({"name": table_name, "columns": columns})
        finally:
            conn.close()
        return {
            "tables": tables,
            "total_tables": len(table_rows),
            "database_type": "SQLite",
            "status": "success",
        }

    def _sql_get_sample_queries(self) -> dict[str, Any]:
        """`sql_get_sample_queries` (sql.py:284-330), including its `execution_time_ms: 0`."""
        self._sample_query_calls += 1
        return {
            "sample_queries": copy.deepcopy(SAMPLE_QUERIES),
            "total_queries": len(SAMPLE_QUERIES),
            "status": "success",
            "execution_time_ms": 0,
        }

    @staticmethod
    def _truncate(statement: str) -> str:
        """Upstream's response echo (sql.py:78-80)."""
        return statement[:100] + "..." if len(statement) > 100 else statement


def create_backend(initial_state: dict[str, Any]) -> Backend:
    """Build a backend seeded from a case's ``initial_state`` (ADAPTER.md)."""
    return Backend(initial_state)
