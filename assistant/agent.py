"""Assistant agent: an OpenAI function-calling loop with a sliding
conversation window plus a rolling summary of older turns."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI, RateLimitError

from assistant.telemetry import TurnMetrics, new_turn_id, turn_id
from assistant.tools import DATE_QUESTION, ToolExecutor
from config import OPENAI_CHAT_MODEL, require_openai_key

log = logging.getLogger("assistant.agent")

MAX_TOOL_ROUNDS = 8  # circuit breaker for runaway tool loops
MAX_HISTORY_TURNS = 6  # sliding window of recent guest turns kept verbatim
MAX_SUMMARY_WORDS = 120  # bound on the rolling summary itself

SUMMARY_SYSTEM_PROMPT = """\
You maintain a compact running summary of an ongoing guest-service chat, \
for the assistant's own later reference — this is never shown to the guest.

Merge the PREVIOUS SUMMARY with the NEW EXCHANGES below into ONE updated \
summary, as short bullet points. Keep only durable facts, stated \
preferences, decisions made, and any specific numbers, dates, or names \
mentioned — drop greetings, small talk, and anything already superseded by \
a later exchange. Keep the whole thing under {max_words} words. Output \
only the updated bullet points, nothing else.
"""


@dataclass
class TurnResult:
    """Everything one guest turn produced."""
    text: str
    citations: list[dict] = field(default_factory=list)
    data_table: Optional[dict] = None
    reservation_card: Optional[dict] = None
    pending_action: Optional[dict] = None
    tools_used: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, system_prompt: str, executor: ToolExecutor,
                 tool_specs: list[dict]):
        require_openai_key()
        self._client = AsyncOpenAI()
        self._executor = executor
        self._tools = tool_specs
        self._system_prompt = system_prompt
        self._summary = ""
        self._messages: list[dict] = []  # conversation only; system prompt is added at request time

    async def chat(self, user_message: str) -> TurnResult:
        """One guest turn: may involve several model/tool rounds internally."""
        self._executor.reset_turn()
        tid = turn_id()
        metrics = TurnMetrics(turn_id=tid if tid != "-" else new_turn_id())
        self._executor.metrics = metrics
        self._messages.append({"role": "user", "content": user_message})
        self._executor.set_recent_guest_messages(self._recent_guest_texts())

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                msg = await self._model_round(metrics, with_tools=True)

                if not msg.tool_calls:
                    self._messages.append({"role": "assistant", "content": msg.content})
                    await self._trim_history()
                    metrics.emit()
                    return self._build_result(msg.content or "")

                self._messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })

                # Calls in one round are independent (dependent calls come
                # in later rounds), so they run concurrently.
                parsed = []
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    parsed.append((tc, args))
                results = await asyncio.gather(
                    *(self._executor.execute(tc.function.name, args)
                      for tc, args in parsed))

                date_missing = False
                for (tc, _), result in zip(parsed, results):
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                    try:
                        if (json.loads(result).get("error", {}).get("code")
                                == "date_not_specified"):
                            date_missing = True
                    except (json.JSONDecodeError, AttributeError):
                        pass

                if date_missing:
                    # Deterministic reply: the model never gets a turn to
                    # propose a default date.
                    self._messages.append(
                        {"role": "assistant", "content": DATE_QUESTION})
                    await self._trim_history()
                    metrics.emit()
                    return self._build_result(DATE_QUESTION)

            msg = await self._model_round(metrics, with_tools=False)
            text = msg.content or (
                "I'm sorry — I wasn't able to complete that just now. "
                "Guest Services on Deck 5 would be happy to help.")
            self._messages.append({"role": "assistant", "content": text})
            await self._trim_history()
            metrics.emit()
            return self._build_result(text)

        except RateLimitError:
            # The OpenAI SDK already retries transient rate limits on its
            # own; the request itself was too large for
            # tokens-per-minute budget, which no amount of
            # retrying fixes. Trim history further rather than resending.
            log.warning("chat_turn_rate_limited")
            self._drop_partial_turn()
            return TurnResult(
                text="We're getting a lot of requests right now and this "
                     "one couldn't go through. Please try a shorter "
                     "question, or try again in a moment.")

        except Exception:
            # A single bad turn (e.g. a transient API error) should not sink
            # the whole conversation. Roll history back to before this turn
            # started — popping only the last message isn't safe here, since
            # the failure may happen after a tool_calls message was already
            # appended but before its matching tool result was, which would
            # break every subsequent request with a mismatched history.
            log.exception("chat_turn_failed")
            self._drop_partial_turn()
            return TurnResult(
                text="I'm sorry — I had trouble with that just now. Could "
                     "you try asking again?")

    async def _model_round(self, metrics: TurnMetrics, *, with_tools: bool):
        """One timed model call; records latency and token usage."""
        start = time.monotonic()
        kwargs = {"model": OPENAI_CHAT_MODEL,
                  "messages": self._outgoing_messages(),
                  "temperature": 0.4}
        if with_tools:
            kwargs["tools"] = self._tools
        response = await self._client.chat.completions.create(**kwargs)
        metrics.add_llm((time.monotonic() - start) * 1000,
                        getattr(response, "usage", None))
        return response.choices[0].message

    def _outgoing_messages(self) -> list[dict]:
        """Build the actual request payload: system prompt (plus the rolling
        summary, if any) followed by the bounded recent-turns window."""
        system_content = self._system_prompt
        if self._summary:
            system_content += (
                "\n\nEARLIER IN THIS CONVERSATION (summarized — the guest "
                "cannot see this; treat it as background you already know):\n"
                + self._summary
            )
        return [{"role": "system", "content": system_content}] + self._messages

    def _drop_partial_turn(self) -> None:
        turn_starts = [i for i, m in enumerate(self._messages) if m["role"] == "user"]
        if turn_starts:
            self._messages = self._messages[:turn_starts[-1]]

    async def _trim_history(self) -> None:
        """Keep only the most recent MAX_HISTORY_TURNS guest turns verbatim.
        Whatever ages out is folded into the rolling summary first, rather
        than simply discarded, so a guest referencing something from many
        turns back still gets a grounded answer."""
        turn_starts = [i for i, m in enumerate(self._messages) if m["role"] == "user"]
        if len(turn_starts) <= MAX_HISTORY_TURNS:
            return
        boundary = turn_starts[-MAX_HISTORY_TURNS]
        dropped, self._messages = self._messages[:boundary], self._messages[boundary:]
        await self._update_summary(dropped)

    async def _update_summary(self, dropped: list[dict]) -> None:
        transcript = self._transcript(dropped)
        if not transcript:
            return
        try:
            response = await self._client.chat.completions.create(
                model=OPENAI_CHAT_MODEL,
                messages=[
                    {"role": "system",
                     "content": SUMMARY_SYSTEM_PROMPT.format(max_words=MAX_SUMMARY_WORDS)},
                    {"role": "user",
                     "content": f"PREVIOUS SUMMARY:\n{self._summary or '(none yet)'}\n\n"
                                f"NEW EXCHANGES:\n{transcript}"},
                ],
                temperature=0,
                max_tokens=220,
            )
            self._summary = (response.choices[0].message.content or self._summary).strip()
        except Exception:
            # Summarizing is a best-effort enhancement, not a safety-critical
            # path — if it fails, keep the previous summary rather than
            # losing it, and let the conversation continue normally.
            log.warning("history_summary_update_failed", exc_info=True)

    @staticmethod
    def _transcript(messages: list[dict]) -> str:
        """Guest-perspective transcript of dropped turns, for summarizing.
        Tool calls/results are internal plumbing and skipped. Synthetic
        stage-direction text (from greet/confirm/cancel flows, and the note
        prepended when a stale pending action is cleared) is parenthesized
        and stripped, so the summary reflects what the guest actually said
        and heard — not the internal prompting used to drive the flow."""
        lines = []
        for m in messages:
            content = (m.get("content") or "").strip()
            if content.startswith("("):
                close = content.find(")\n")
                content = content[close + 2:].strip() if close != -1 else ""
            if not content:
                continue
            if m["role"] == "user":
                lines.append(f"Guest: {content}")
            elif m["role"] == "assistant":
                lines.append(f"Assistant: {content}")
        return "\n".join(lines)

    def _recent_guest_texts(self) -> list[str]:
        """Guest-authored text from recent turns, stage directions stripped.
        Guest-only on purpose: a model-proposed date confirmed by a bare
        \"yes\" must not count as guest-stated."""
        texts = []
        for m in self._messages:
            if m["role"] != "user":
                continue
            content = (m.get("content") or "").strip()
            if content.startswith("("):
                close = content.find(")\n")
                content = content[close + 2:].strip() if close != -1 else ""
            if content:
                texts.append(content)
        return texts

    def _build_result(self, text: str) -> TurnResult:
        turn = self._executor.turn
        return TurnResult(
            text=text,
            citations=turn.citations,
            data_table=turn.data_table,
            reservation_card=turn.reservation_card,
            pending_action=turn.pending_action,
            tools_used=list(turn.tools_used),
        )
