"""Text2SQL pipeline: question -> generate SQL -> validate (AST) ->
guest scope -> execute. One retry on invalid SQL; the SQL is logged but
never returned to the assistant or the browser."""

import logging
import time

from text2sql.executor import SQLExecutionError, execute
from text2sql.generator import SQLGenerator
from text2sql.validator import SQLValidationError, validate

log = logging.getLogger("text2sql.service")


class Text2SQLService:
    def __init__(self, today: str):
        self._generator = SQLGenerator(today)

    def answer_question(self, question: str, guest_id: str) -> dict:
        """Answer a natural-language data question for one verified guest.

        Returns {columns, rows, row_count, result_type} on success or
        {error: {code, message}} on failure. Never includes the SQL.
        """
        error_feedback = None
        for attempt in (1, 2):
            try:
                gen_start = time.monotonic()
                generated = self._generator.generate(question, error_feedback)
                gen_ms = (time.monotonic() - gen_start) * 1000
                validated = validate(generated["sql"])
                log.info("text2sql attempt=%d guest=%s sql=%s",
                         attempt, guest_id, validated)
                exec_start = time.monotonic()
                result = execute(validated, guest_id,
                                 result_type=generated["result_type"])
                exec_ms = (time.monotonic() - exec_start) * 1000
                log.info("text2sql rows=%d gen=%.0fms db=%.0fms",
                         result["row_count"], gen_ms, exec_ms)
                result["timings_ms"] = {"sql_generation": round(gen_ms, 1),
                                        "db_execution": round(exec_ms, 1)}
                return result
            except (SQLValidationError, SQLExecutionError) as err:
                log.warning("text2sql attempt=%d rejected: %s", attempt, err)
                error_feedback = str(err)
            except Exception:
                log.exception("text2sql unexpected failure")
                break

        return {"error": {
            "code": "query_failed",
            "message": ("The guest-data query could not be completed. "
                        "Answer from other tools or suggest Guest Services."),
        }}
