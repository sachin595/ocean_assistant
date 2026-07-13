"""
SQL validation — abstract-syntax-tree inspection with sqlglot.

Model-generated SQL is never executed as received. It is parsed, its tree
is walked, and it is re-serialized from the tree, so string tricks (comment
smuggling, casing, whitespace) cannot slip anything past the checks.
"""

import sqlglot
from sqlglot import exp

from text2sql.schema import APPROVED_TABLES

MAX_ROWS = 100

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Attach, exp.Detach, exp.Pragma, exp.Command, exp.Transaction,
)

FORBIDDEN_FUNCTIONS = {
    "load_extension", "readfile", "writefile", "edit",
    "fts3_tokenizer", "zipfile",
}


class SQLValidationError(Exception):
    pass


def validate(sql: str) -> str:
    """Validate model-generated SQL and return it normalized, with a row
    limit guaranteed. Raises SQLValidationError with a specific reason."""
    if not sql or not sql.strip():
        raise SQLValidationError("Empty query.")

    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as err:
        raise SQLValidationError(f"SQL could not be parsed: {err}") from err

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLValidationError("Exactly one statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SQLValidationError(
            f"Only SELECT queries are allowed, got {type(tree).__name__.upper()}.")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLValidationError(
                f"Forbidden operation: {type(node).__name__.upper()}.")

    if list(tree.find_all(exp.CTE)):
        raise SQLValidationError("WITH clauses are not allowed; use subqueries.")

    for table in tree.find_all(exp.Table):
        if table.db or table.catalog:
            raise SQLValidationError(
                "Database-qualified table names are not allowed.")
        if table.name.lower() not in APPROVED_TABLES:
            raise SQLValidationError(f"Table '{table.name}' is not approved.")

    for func in tree.find_all(exp.Anonymous):
        if str(func.this).lower() in FORBIDDEN_FUNCTIONS:
            raise SQLValidationError(f"Function '{func.this}' is not allowed.")
    for func in tree.find_all(exp.Func):
        name = (func.sql_name() or "").lower()
        if name in FORBIDDEN_FUNCTIONS:
            raise SQLValidationError(f"Function '{name}' is not allowed.")

    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(MAX_ROWS)
    else:
        try:
            if int(limit.expression.this) > MAX_ROWS:
                tree.set("limit", None)
                tree = tree.limit(MAX_ROWS)
        except (AttributeError, TypeError, ValueError):
            tree.set("limit", None)
            tree = tree.limit(MAX_ROWS)

    return tree.sql(dialect="sqlite")
