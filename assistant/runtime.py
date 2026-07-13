"""
AssistantRuntime — shared startup and session factory.

Owns everything expensive and shared across guests: the RAG retriever, the
MCP subprocess and client session, the discovered tool specs, and the
Text2SQL service. Interfaces (CLI, web) start one runtime and create one
AssistantSession per logged-in guest.
"""

import logging
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from assistant.guests import get_guest
from assistant.session import AssistantSession
from config import ASSISTANT_TODAY, PROJECT_ROOT, require_openai_key
from rag.retriever import Retriever
from text2sql.service import Text2SQLService

log = logging.getLogger("assistant.runtime")


class UnknownGuestError(Exception):
    pass


class AssistantRuntime:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._mcp: ClientSession | None = None
        self._retriever: Retriever | None = None
        self._text2sql: Text2SQLService | None = None
        self._tool_specs: list[dict] | None = None
        self.today = ASSISTANT_TODAY
        self.friendly_today = datetime.strptime(
            ASSISTANT_TODAY, "%Y-%m-%d").strftime("%B %d, %Y")

    async def start(self) -> None:
        require_openai_key()
        self._retriever = Retriever()
        self._text2sql = Text2SQLService(self.today)

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "mcp_server" / "dining_mcp.py")],
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._mcp = await self._stack.enter_async_context(
            ClientSession(read, write))
        await self._mcp.initialize()
        log.info("Runtime started: retriever, Text2SQL, and MCP session ready.")

    async def stop(self) -> None:
        await self._stack.aclose()
        log.info("Runtime stopped.")

    async def create_session(self, guest_id: str) -> AssistantSession:
        """Create a per-guest session. Raises UnknownGuestError for IDs not
        found in the guest profiles."""
        guest = get_guest(guest_id)
        if not guest:
            raise UnknownGuestError(guest_id)
        if self._tool_specs is None:
            self._tool_specs = await self._probe_tool_specs(guest)
        return AssistantSession(
            guest=guest,
            mcp_session=self._mcp,
            retriever=self._retriever,
            text2sql=self._text2sql,
            tool_specs=self._tool_specs,
            friendly_today=self.friendly_today,
        )

    async def _probe_tool_specs(self, guest: dict) -> list[dict]:
        from assistant.tools import ToolExecutor
        probe = ToolExecutor(self._mcp, self._retriever, self._text2sql, guest)
        specs = await probe.load_tool_specs()
        log.info("Discovered %d tool specs.", len(specs))
        return specs
