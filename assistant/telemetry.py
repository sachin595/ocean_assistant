"""Per-turn observability: stage timings, token usage, tool outcomes, and
a turn ID (contextvar) stamped on every log line."""

import logging
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

current_turn_id: ContextVar[str] = ContextVar("current_turn_id", default="-")

log = logging.getLogger("telemetry")

# Process-wide counters. Guardrail rejections (deliberate refusals) are
# tracked separately from tool failures (exceptions, broken calls).
_tool_calls_total = 0
_guardrail_rejections_total = 0
_tool_failures_total = 0


def record_tool_outcome(outcome: str) -> None:
    """Accumulate one tool call's outcome: 'ok', 'guardrail', or 'failure'."""
    global _tool_calls_total, _guardrail_rejections_total, _tool_failures_total
    _tool_calls_total += 1
    if outcome == "guardrail":
        _guardrail_rejections_total += 1
    elif outcome == "failure":
        _tool_failures_total += 1


def failure_stats() -> dict:
    """Process-lifetime tool call statistics."""
    rate = (_tool_failures_total / _tool_calls_total * 100
            if _tool_calls_total else 0.0)
    return {
        "tool_calls": _tool_calls_total,
        "guardrail_rejections": _guardrail_rejections_total,
        "tool_failures": _tool_failures_total,
        "tool_failure_rate": f"{rate:.1f}%",
    }

# Safe to log verbatim; everything else is reduced to a character count.
LOGGABLE_ARG_KEYS = {
    "venue", "venue_id", "reservation_id", "reservation_date", "reservation_time",
    "party_size", "date", "status", "from_date", "to_date", "action_id",
}


def new_turn_id() -> str:
    turn_id = f"t_{secrets.token_hex(4)}"
    current_turn_id.set(turn_id)
    return turn_id


def turn_id() -> str:
    return current_turn_id.get()


def redact_args(args: dict) -> dict:
    """Keep loggable keys; reduce everything else to a length."""
    redacted = {}
    for key, value in args.items():
        if key in LOGGABLE_ARG_KEYS:
            redacted[key] = value
        elif isinstance(value, str):
            redacted[key] = f"<{len(value)} chars>"
        else:
            redacted[key] = f"<{type(value).__name__}>"
    return redacted


@dataclass
class TurnMetrics:
    """Timings and counters for one guest turn."""
    turn_id: str
    started: float = field(default_factory=time.monotonic)
    llm_ms: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stage_ms: dict = field(default_factory=dict)   # e.g. rag, sql_gen, db_exec, mcp
    tools: list = field(default_factory=list)       # tool names in call order
    tool_failures: int = 0       # exceptions, broken calls — worth investigating
    guardrail_rejections: int = 0   # deliberate refusals: safety design working

    def add_llm(self, elapsed_ms: float, usage) -> None:
        self.llm_ms += elapsed_ms
        self.llm_calls += 1
        if usage is not None:
            self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

    def add_stage(self, stage: str, elapsed_ms: float) -> None:
        self.stage_ms[stage] = round(self.stage_ms.get(stage, 0.0) + elapsed_ms, 1)

    def add_tool(self, name: str, elapsed_ms: float, outcome: str) -> None:
        """Record one tool call: outcome is 'ok', 'guardrail', or 'failure'."""
        self.tools.append(name)
        if outcome == "failure":
            self.tool_failures += 1
        elif outcome == "guardrail":
            self.guardrail_rejections += 1
        self.add_stage(f"tool.{name}", elapsed_ms)

    @contextmanager
    def stage(self, name: str):
        start = time.monotonic()
        try:
            yield
        finally:
            self.add_stage(name, (time.monotonic() - start) * 1000)

    def summary(self) -> str:
        total_ms = (time.monotonic() - self.started) * 1000
        stages = " ".join(f"{k}={v}ms" for k, v in sorted(self.stage_ms.items()))
        return (f"turn={self.turn_id} total={total_ms:.0f}ms "
                f"llm={self.llm_ms:.0f}ms/{self.llm_calls}calls "
                f"tokens={self.prompt_tokens}in/{self.completion_tokens}out "
                f"tools=[{','.join(self.tools) or '-'}] "
                f"failures={self.tool_failures} "
                f"guardrails={self.guardrail_rejections} {stages}")

    def emit(self) -> None:
        log.info(self.summary())


class _TurnIdFilter(logging.Filter):
    """Injects the current turn ID into every record as %(turn_id)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.turn_id = current_turn_id.get()
        return True


def configure_logging(log_path: Path, level: int = logging.INFO) -> None:
    """Rotating file logging (1 MB x 3 backups); lines carry the turn ID."""
    handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(turn_id)s] %(name)s: %(message)s"))
    handler.addFilter(_TurnIdFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
