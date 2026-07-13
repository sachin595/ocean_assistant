"""
Text2SQL guardrails — offline tests (no API key, no services).

Covers the security layers that must hold regardless of what the SQL
generator produces: AST validation, guest scoping, the row cap, and the
read-only connection. Run: `pytest tests/test_text2sql_guardrails.py -v`
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from data import database
from text2sql.executor import build_scoped_sql, execute
from text2sql.validator import MAX_ROWS, SQLValidationError, validate


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    test_db = tmp_path_factory.mktemp("t2s") / "test.db"
    original = database.DB_PATH
    database.DB_PATH = test_db
    database.init_database()
    yield test_db
    database.DB_PATH = original


# ── Validator rejections ───────────────────────────────────────────────────

REJECTED = [
    "DELETE FROM folio_transactions",
    "UPDATE guest_profiles SET loyalty_points = 999999",
    "DROP TABLE reservations",
    "SELECT * FROM main.guest_profiles",
    "SELECT 1; SELECT 2",
    "PRAGMA table_info(guest_profiles)",
    "ATTACH DATABASE 'evil.db' AS evil",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM reservations",
    "WITH x AS (SELECT * FROM folio_transactions) SELECT * FROM x",
    "SELECT load_extension('evil')",
    "INSERT INTO folio_transactions (transaction_id) VALUES ('x')",
    "CREATE TABLE pwned (id INTEGER)",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_dangerous_sql_is_rejected(sql):
    with pytest.raises(SQLValidationError):
        validate(sql)


def test_accepted_sql_is_normalized_with_limit():
    out = validate("SELECT category, SUM(total) AS total_spent "
                   "FROM folio_transactions GROUP BY category")
    assert out.upper().startswith("SELECT")
    assert f"LIMIT {MAX_ROWS}" in out


def test_oversized_limit_is_clamped():
    out = validate("SELECT * FROM folio_transactions LIMIT 5000")
    assert f"LIMIT {MAX_ROWS}" in out


def test_only_approved_logical_tables_are_accessible():
    validate("SELECT * FROM dining_reservations")
    validate("SELECT * FROM guest_profiles")
    validate("SELECT * FROM folio_transactions")
    with pytest.raises(SQLValidationError):
        validate("SELECT * FROM guest_secrets")


# ── Guest scoping ──────────────────────────────────────────────────────────

def _all_guest_ids(db, table):
    conn = sqlite3.connect(db)
    ids = {r[0] for r in conn.execute(f"SELECT DISTINCT guest_id FROM {table}")}
    conn.close()
    return ids


def test_missing_where_clause_still_scopes_to_guest(db):
    result = execute(validate("SELECT DISTINCT guest_id FROM folio_transactions"),
                     "G100036", db_path=db)
    assert result["rows"] == [["G100036"]]
    assert len(_all_guest_ids(db, "folio_transactions")) > 1


def test_guest_with_no_rows_sees_nothing(db):
    result = execute(validate("SELECT COUNT(*) AS n FROM folio_transactions"),
                     "G100005", db_path=db)
    assert result["rows"][0][0] == 0


def test_scope_applies_to_all_three_tables(db):
    for table in ("guest_profiles", "folio_transactions", "dining_reservations"):
        result = execute(validate(f"SELECT DISTINCT guest_id FROM {table}"),
                         "G100001", db_path=db)
        assert all(row == ["G100001"] for row in result["rows"])


def test_filtering_for_another_guest_returns_nothing(db):
    result = execute(
        validate("SELECT * FROM folio_transactions WHERE guest_id = 'G100165'"),
        "G100036", db_path=db)
    assert result["row_count"] == 0


def test_scope_wrapper_shape():
    scoped = build_scoped_sql("SELECT COUNT(*) FROM folio_transactions")
    assert scoped.count(":verified_guest_id") == 3
    assert "main.reservations" in scoped


# ── Execution limits & read-only ───────────────────────────────────────────

def test_row_cap_enforced(db):
    result = execute(validate("SELECT * FROM folio_transactions"),
                     "G100036", db_path=db)
    assert result["row_count"] <= MAX_ROWS


def test_readonly_connection_cannot_write(db):
    conn = database.get_readonly_conn(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE guest_profiles SET loyalty_points = 0")
    conn.close()


def test_scalar_result_type_inferred(db):
    result = execute(validate("SELECT COUNT(*) AS n FROM dining_reservations"),
                     "G100001", db_path=db)
    assert result["result_type"] == "scalar"
    assert result["columns"] == ["n"]
