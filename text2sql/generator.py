"""
SQL generation — a dedicated, specialized OpenAI call.

Kept separate from the main assistant so the schema and SQL rules never mix
into the guest-facing prompt, and the guest-facing persona never leaks into
SQL generation. Returns a structured object: {"sql": ..., "result_type": ...}.
"""

import json

from openai import OpenAI

from config import OPENAI_SQL_MODEL, require_openai_key
from text2sql.schema import schema_prompt

RULES = """
Return ONLY a JSON object: {"sql": "<query>", "result_type": "scalar" | "table"}
  - result_type "scalar" when the answer is a single value (one row, one column)
  - result_type "table" otherwise

The SQL must obey ALL of these rules:
  - Exactly ONE SELECT statement (UNION of SELECTs is allowed)
  - SQLite syntax only
  - Only the three approved tables and their listed columns
  - No WITH clauses; use subqueries if needed
  - No database-qualified names (never main.table or temp.table)
  - No semicolons, no comments, no markdown fences
  - No INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, PRAGMA, or ATTACH
  - Never filter by or select literal guest identifiers
  - Use readable column aliases (e.g. SUM(total) AS total_spent)
  - Round money to 2 decimals: ROUND(SUM(total), 2)

Examples:
  Q: "How much have I spent at the spa?"
  {"sql": "SELECT ROUND(SUM(total), 2) AS spa_spend FROM folio_transactions WHERE category = 'Spa'", "result_type": "scalar"}

  Q: "How much did I spend by category?"
  {"sql": "SELECT category, ROUND(SUM(total), 2) AS total_spent FROM folio_transactions GROUP BY category ORDER BY total_spent DESC", "result_type": "table"}

  Q: "How many confirmed dining reservations do I have coming up?"
  {"sql": "SELECT COUNT(*) AS upcoming_reservations FROM dining_reservations WHERE status = 'Confirmed' AND reservation_date >= '{today}'", "result_type": "scalar"}

  Q: "How many reservations do I have at each restaurant?"
  {"sql": "SELECT venue_name, COUNT(*) AS reservation_count FROM dining_reservations WHERE status = 'Confirmed' GROUP BY venue_name ORDER BY reservation_count DESC", "result_type": "table"}
"""


class SQLGenerator:
    def __init__(self, today: str):
        require_openai_key()
        self._client = OpenAI()
        self._system = schema_prompt(today) + RULES.replace("{today}", today)

    def generate(self, question: str, previous_error: str | None = None) -> dict:
        """Generate SQL for a natural-language question.

        `previous_error` carries the validator/executor message on the single
        retry so the model can correct itself.
        """
        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": question},
        ]
        if previous_error:
            messages.append({
                "role": "user",
                "content": (f"The previous query was rejected: {previous_error}. "
                            "Produce a corrected query following all rules."),
            })
        response = self._client.chat.completions.create(
            model=OPENAI_SQL_MODEL,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content)
        sql = str(payload.get("sql", "")).strip().strip(";").strip()
        result_type = payload.get("result_type", "table")
        if result_type not in {"scalar", "table"}:
            result_type = "table"
        return {"sql": sql, "result_type": result_type}
