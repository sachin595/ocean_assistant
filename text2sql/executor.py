"""SQL execution: guest scoping via CTE rewrite, read-only connection,
row/time limits, and column-kind tagging for display formatting."""

import re
import time
from pathlib import Path
from typing import Optional

import sqlglot
import sqlglot.expressions as exp

from data.database import get_readonly_conn
from text2sql.validator import MAX_ROWS

SCOPE_WRAPPER = """WITH
guest_profiles AS (
    SELECT * FROM main.guest_profiles WHERE guest_id = :verified_guest_id
),
folio_transactions AS (
    SELECT * FROM main.folio_transactions WHERE guest_id = :verified_guest_id
),
dining_reservations AS (
    SELECT r.* FROM main.reservations r
    JOIN main.guest_profiles p ON p.guest_id = r.guest_id
    WHERE r.guest_id = :verified_guest_id
      AND r.guest_name = p.first_name || ' ' || p.last_name
)
{query}"""

TIMEOUT_SECONDS = 5.0


class SQLExecutionError(Exception):
    pass


def build_scoped_sql(validated_sql: str) -> str:
    """Wrap a validated SELECT so every approved table name resolves to a
    guest-filtered CTE."""
    return SCOPE_WRAPPER.format(query=validated_sql)


PERCENT_NAME_PATTERN = re.compile(r"percent|percentage|pct|ratio|rate", re.IGNORECASE)


def _column_kinds(validated_sql: str, columns: list[str]) -> list[Optional[str]]:
    """Tag each column as count/currency/percent from the OUTERMOST
    operation: COUNT is a count, top-level SUM/AVG is money, arithmetic
    over aggregates is a ratio (percent when the column name says so)."""
    try:
        tree = sqlglot.parse_one(validated_sql, read="sqlite")
    except Exception:
        return [None] * len(columns)

    def unwrap(node):
        while isinstance(node, (exp.Alias, exp.Paren, exp.Round, exp.Cast)):
            node = node.this
        return node

    kinds: list[Optional[str]] = []
    selects = tree.find_all(exp.Select)
    top_select = next(selects, None)
    expressions = top_select.expressions if top_select else []
    for i, column in enumerate(columns):
        kind = None
        if i < len(expressions):
            node = unwrap(expressions[i])
            if isinstance(node, exp.Count):
                kind = "count"
            elif isinstance(node, (exp.Sum, exp.Avg)):
                kind = "currency"
            elif PERCENT_NAME_PATTERN.search(column or ""):
                kind = "percent"
        kinds.append(kind)
    return kinds


def execute(validated_sql: str, guest_id: str,
            result_type: str = "table",
            db_path: Optional[Path] = None) -> dict:
    """Execute validated SQL scoped to one guest. Returns
    {columns, rows, row_count, result_type, column_kinds}."""
    scoped = build_scoped_sql(validated_sql)
    conn = get_readonly_conn(db_path)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    conn.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline else 0, 50_000)
    try:
        cursor = conn.execute(scoped, {"verified_guest_id": guest_id})
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = [list(r) for r in cursor.fetchmany(MAX_ROWS)]
    except Exception as err:
        raise SQLExecutionError(str(err)) from err
    finally:
        conn.close()

    if len(rows) == 1 and len(columns) == 1:
        result_type = "scalar"
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "result_type": result_type,
        "column_kinds": _column_kinds(validated_sql, columns),
    }
