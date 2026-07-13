"""
AssistantSession — one logged-in guest's conversation.

Holds the guest profile, the per-guest tool executor (identity injection
and pending-action storage), the agent (conversation history), and the
confirm/cancel entry points that execute or discard staged actions. Both
the CLI and the web app drive sessions through this class.
"""

import json
import re
import time
from typing import Optional

from assistant.agent import Agent, TurnResult
from assistant.guests import profile_summary
from assistant.telemetry import new_turn_id
from assistant.prompts import build_system_prompt
from assistant.tools import ToolExecutor


AFFIRMATION_PATTERN = re.compile(
    r"^\s*(yes|yes[.!,]* please|yes please|yep|yeah|y|sure|ok|okay|confirm|"
    r"confirmed|go ahead|please confirm( that)?|yes[,;]? confirm( it| that)?|"
    r"sounds good|do it|book it|proceed)\s*[.!]*\s*$", re.IGNORECASE)

DECLINE_PATTERN = re.compile(
    r"^\s*(no|nope|no thanks|not now|cancel( that| it)?|don'?t|"
    r"never ?mind|leave it)\s*[.!]*\s*$", re.IGNORECASE)

GREETING_PATTERN = re.compile(
    r"^\s*(hi|hii+|hello|hey|heyy+|good (morning|afternoon|evening)|"
    r"hola|yo)\s*[.!]*\s*$", re.IGNORECASE)

THANKS_PATTERN = re.compile(
    r"^\s*(thanks?|thank you|thanks a lot|thank you so much|thx|ty)"
    r"\s*[.!]*\s*$", re.IGNORECASE)


class AssistantSession:
    def __init__(self, *, guest: dict, mcp_session, retriever, text2sql,
                 tool_specs: list[dict], friendly_today: str):
        self.guest = guest
        self.executor = ToolExecutor(mcp_session, retriever, text2sql, guest)
        system_prompt = build_system_prompt(
            profile_summary(guest), friendly_today)
        self.agent = Agent(system_prompt, self.executor, tool_specs)
        self.last_activity = time.time()

    @property
    def guest_name(self) -> str:
        return f"{self.guest['First_Name']} {self.guest['Last_Name']}"

    def touch(self) -> None:
        self.last_activity = time.time()

    async def greet(self) -> TurnResult:
        self.touch()
        new_turn_id()
        return await self.agent.chat(
            "(The guest just logged in — greet them warmly by name and "
            "briefly offer your help with ship information, their onboard "
            "account, and dining reservations. Do not use any tools.)")

    async def chat(self, message: str) -> TurnResult:
        """One guest turn. Trivial turns (typed yes/no with a pending action,
        greetings, thanks) resolve deterministically without a model call;
        anything else clears stale pending actions and goes to the agent."""
        self.touch()
        new_turn_id()

        if self.executor.has_pending:
            action_id = self.executor.pending_action_id
            if AFFIRMATION_PATTERN.match(message):
                return await self.confirm_pending(action_id)
            if DECLINE_PATTERN.match(message):
                return await self.cancel_pending(action_id)

        canned = self._trivial_reply(message)
        if canned is not None:
            return canned

        dropped = self.executor.clear_pending()
        if dropped:
            message = (f"(Note: the previously staged action was cleared "
                       f"because the guest continued the conversation.)\n"
                       f"{message}")
        return await self.agent.chat(message)

    def _trivial_reply(self, message: str) -> Optional[TurnResult]:
        """Canned replies for greetings and thanks."""
        if GREETING_PATTERN.match(message):
            return TurnResult(
                text=f"Hello, {self.guest['First_Name']}! 🌊 How can I help "
                     f"— ship information, your onboard account, or a "
                     f"dining reservation?")
        if THANKS_PATTERN.match(message):
            return TurnResult(
                text="You're very welcome! If anything else comes up, "
                     "I'm right here. 🌊")
        return None

    async def confirm_pending(self, action_id: str) -> TurnResult:
        """Execute the staged action with its exact stored arguments, then
        let the model present the outcome — and continue with the next
        action if the guest's request had more than one."""
        self.touch()
        new_turn_id()
        result = await self.executor.run_pending(action_id)
        pretty = self._friendly_result(result)
        return await self.agent.chat(
            f"(The guest pressed Confirm for action {action_id}. "
            f"The system executed it. Result: {pretty}. "
            f"Present the outcome warmly — include the confirmation number "
            f"if present; if it failed, explain kindly and offer options. "
            f"If the guest's request included further actions that have not "
            f"been staged yet, stage the NEXT one now by calling the "
            f"appropriate tool, and tell the guest it too needs their "
            f"confirmation. If nothing remains, do not call any tools.)")

    async def cancel_pending(self, action_id: str) -> TurnResult:
        self.touch()
        new_turn_id()
        self.executor.clear_pending()
        return await self.agent.chat(
            f"(The guest declined action {action_id}; it was discarded. "
            f"Acknowledge briefly and offer to adjust or help with anything "
            f"else. Do not call any tools.)")

    @property
    def has_pending(self) -> bool:
        return self.executor.has_pending

    @staticmethod
    def _friendly_result(result: str) -> str:
        try:
            return json.dumps(json.loads(result))
        except json.JSONDecodeError:
            return result


def turn_to_dict(turn: TurnResult) -> dict:
    """Serialize a TurnResult for JSON transport to the web client."""
    return {
        "text": turn.text,
        "citations": turn.citations,
        "data_table": turn.data_table,
        "reservation_card": turn.reservation_card,
        "pending_action": turn.pending_action,
    }
