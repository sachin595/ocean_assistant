"""Tool layer between the LLM and the system: RAG search, Text2SQL, and
dining operations over MCP. State-changing calls are staged for explicit
guest confirmation; guards here validate before anything is shown."""

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp import ClientSession

from assistant.telemetry import record_tool_outcome, redact_args
from rag.citation_selector import select_sources
from rag.retriever import Retriever
from text2sql.service import Text2SQLService

log = logging.getLogger("assistant.tools")

MAX_PASSAGE_CHARS = 1200  # keeps individual RAG turns from bloating context

STATE_CHANGING = {"create_reservation", "modify_reservation", "cancel_reservation"}
GUEST_SCOPED = {"list_reservations", "create_reservation",
                "modify_reservation", "cancel_reservation"}
MAX_PARTY_SIZE = 8
RECENT_MESSAGES_WINDOW = 6  # recent guest turns that count as "still talking about this booking"

# Asked verbatim when a booking is attempted without a guest-stated date.
DATE_QUESTION = ("What date would you like the reservation for? You can say "
                 "tonight, tomorrow, or a specific date like February 9.")

DATE_SIGNAL_PATTERN = re.compile(
    r"\b("
    r"today|tonight|tomorrow|tmrw|"
    r"mon|tue|tues|wed|thu|thurs|fri|sat|sun|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|"
    r"october|november|december|"
    r"next week|this week|in \d+ days?"
    r")\b"
    r"|\b\d{1,2}(st|nd|rd|th)\b"       # "the 8th"
    r"|\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b"  # 2/8, 02-08-2026
    r"|\b\d{4}-\d{2}-\d{2}\b",          # 2026-02-08
    re.IGNORECASE,
)

RAG_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "search_knowledge_base",
        "description": (
            "Search the ship's knowledge base (12 documents covering ship "
            "info & policies, dining venues, entertainment, spa & wellness, "
            "ports, shore excursions, loyalty program, billing policies, "
            "guest services, reservations policy, and escalation). Use for "
            "ANY question about the ship, its services, prices, hours, or "
            "policies. Returns relevant passages with their source document "
            "for citation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The guest's question, rephrased as a "
                                   "focused search query.",
                }
            },
            "required": ["query"],
        },
    },
}

TEXT2SQL_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "query_guest_data",
        "description": (
            "Answer questions about the logged-in guest's own data: their "
            "profile, cabin, loyalty points, folio charges, spending, "
            "transaction status (Posted/Pending/Disputed/Refunded), and "
            "analytical questions involving their reservations (counts, "
            "totals, breakdowns, largest/smallest, date ranges). "
            "This is the ONLY correct tool for any counting question — "
            "'how many reservations', totals, per-restaurant or per-month "
            "breakdowns — because it computes the number in SQL exactly; "
            "never count list_reservations rows by hand instead. "
            "Do not use it for general ship policies — use "
            "search_knowledge_base. Do not use it to create, modify, or "
            "cancel reservations — use the dining tools. "
            "Pass the guest's question in natural language; the system "
            "handles the data access."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The guest's data question, in natural "
                                   "language, self-contained.",
                }
            },
            "required": ["question"],
        },
    },
}


@dataclass
class TurnArtifacts:
    """Structured items collected while executing one guest turn."""
    citations: list[dict] = field(default_factory=list)
    data_table: Optional[dict] = None
    reservation_card: Optional[dict] = None
    pending_action: Optional[dict] = None
    tools_used: list[str] = field(default_factory=list)


class ToolExecutor:
    """Executes LLM tool calls: RAG and Text2SQL locally, dining via MCP."""

    def __init__(self, mcp_session: ClientSession, retriever: Retriever,
                 text2sql: Text2SQLService, guest: dict):
        self._mcp = mcp_session
        self._retriever = retriever
        self._text2sql = text2sql
        self._guest = guest
        self._pending: dict[str, tuple[str, dict]] = {}
        self._recent_guest_messages: list[str] = []
        self._booking_draft: dict = {}
        self.turn = TurnArtifacts()
        self.metrics = None  # TurnMetrics, attached per turn by the Agent

    def set_recent_guest_messages(self, messages: list[str]) -> None:
        """Recent guest-authored messages. Guest-only on purpose: a model-
        proposed date confirmed with a bare \"yes\" must not count."""
        self._recent_guest_messages = messages[-RECENT_MESSAGES_WINDOW:]

    # ── Discovery ─────────────────────────────────────────────────────────

    async def load_tool_specs(self) -> list[dict]:
        """Local tools + MCP tools converted to OpenAI function specs."""
        specs = [RAG_TOOL_SPEC, TEXT2SQL_TOOL_SPEC]
        listed = await self._mcp.list_tools()
        for tool in listed.tools:
            specs.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            })
        return specs

    # ── Turn lifecycle ────────────────────────────────────────────────────

    def reset_turn(self) -> None:
        self.turn = TurnArtifacts()

    def clear_pending(self) -> Optional[str]:
        """Drop any stored pending action (e.g. the guest kept talking
        instead of confirming). Returns the dropped action_id, if any."""
        if not self._pending:
            return None
        action_id = next(iter(self._pending))
        self._pending.clear()
        return action_id

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def pending_action_id(self) -> Optional[str]:
        return next(iter(self._pending), None)

    # ── Execution ─────────────────────────────────────────────────────────

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool call; always returns a JSON string for the LLM."""
        log.info("tool_call name=%s args=%s",
                 name, json.dumps(redact_args(arguments)))
        self.turn.tools_used.append(name)
        start = time.monotonic()
        result = await self._execute_inner(name, arguments)
        elapsed_ms = (time.monotonic() - start) * 1000
        outcome = self._classify_outcome(result)
        record_tool_outcome(outcome)
        if self.metrics is not None:
            self.metrics.add_tool(name, elapsed_ms, outcome)
        log.info("tool_done name=%s outcome=%s elapsed=%.0fms",
                 name, outcome, elapsed_ms)
        return result

    @staticmethod
    def _classify_outcome(result: str) -> str:
        """Classify a result: business error codes are guardrail refusals,
        not failures; only 'tool_failure' (an exception) is a failure."""
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return "failure"
        error = payload.get("error") if isinstance(payload, dict) else None
        if not error:
            return "ok"
        if isinstance(error, dict) and error.get("code") == "tool_failure":
            return "failure"
        return "guardrail"

    async def _execute_inner(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            if name == "search_knowledge_base":
                return self._run_rag(arguments.get("query", ""))

            if name == "query_guest_data":
                return self._run_text2sql(arguments.get("question", ""))

            if name in STATE_CHANGING:
                oversized = self._reject_oversized_party(name, arguments)
                if oversized:
                    return oversized
                precheck_error, target = self._precheck_target(name, arguments)
                if precheck_error:
                    return precheck_error
                return self._stage_pending(name, dict(arguments), target)

            if name in {"list_reservations", "check_availability"} and arguments.get("venue_id"):
                from api.store import RESTAURANTS, resolve_venue_ref
                venue = resolve_venue_ref(arguments["venue_id"])
                if venue is None:
                    return json.dumps({"error": {
                        "code": "unknown_venue",
                        "message": (f"'{arguments['venue_id']}' doesn't match "
                                    f"any venue by name or ID. Valid venues: "
                                    + ", ".join(f"{v['name']} ({vid})"
                                                for vid, v in RESTAURANTS.items())
                                    + ". If the guest's wording is ambiguous, "
                                      "ask which restaurant they mean.")}})
                arguments["venue_id"] = venue["venue_id"]

            if name == "check_availability" and not self._has_date_signal():
                # Same rule as booking: the date must come from the guest.
                log.info("check_availability_blocked reason=no_guest_date")
                return json.dumps({"error": {
                    "code": "date_not_specified",
                    "message": "The guest has not stated a date."}})

            result = await self._call_mcp(name, dict(arguments))
            if name == "list_reservations":
                result = self._attach_long_list_table(result)
            return result

        except Exception as exc:  # noqa: BLE001 — LLM gets a readable error, guest never sees it
            log.exception("tool_error name=%s", name)
            return json.dumps({"error": {
                "code": "tool_failure",
                "message": f"The {name} tool is temporarily unavailable ({exc})."}})

    LONG_LIST_THRESHOLD = 10

    def _attach_long_list_table(self, result: str) -> str:
        """Attach a table card for long lists so every row is guaranteed to
        render; the note tells the model to summarize, not re-list."""
        try:
            payload = json.loads(result)
            reservations = payload.get("reservations")
        except (json.JSONDecodeError, AttributeError):
            return result
        if not reservations or len(reservations) < self.LONG_LIST_THRESHOLD:
            return result
        self.turn.data_table = {
            "caption": "Your reservations — complete list",
            "columns": ["venue", "date", "time", "party", "status", "confirmation"],
            "rows": [[r.get("venue_name"), r.get("reservation_date"),
                      r.get("reservation_time"), r.get("party_size"),
                      r.get("status"), r.get("confirmation_number")]
                     for r in reservations],
            "row_count": len(reservations),
            "result_type": "table",
            "column_kinds": [None, None, None, "count", None, None],
        }
        payload["note"] = (
            f"A complete table of all {len(reservations)} reservations is "
            f"already displayed to the guest. Give a one-or-two sentence "
            f"summary (e.g. the count and date range) — do NOT enumerate "
            f"the reservations again in prose.")
        return json.dumps(payload)

    def _run_rag(self, query: str) -> str:
        results = self._retriever.search(query)
        if not results:
            # Nothing passed the relevance threshold. Better to say so than
            # to let the model answer from weak context or general knowledge.
            return json.dumps({"results": [], "note": (
                "No knowledge-base passage was a confident match for this "
                "question. Tell the guest you're not certain you have the "
                "right onboard information, invite them to rephrase or add "
                "detail, and mention Guest Services (Deck 5) can verify. "
                "Do NOT answer from general knowledge.")})

        selected = select_sources(query, results)
        keep = [r for r in results if r["source_document"] in selected]

        # One citation per selected document, in the order they were
        # chosen. The model only ever sees the passages behind these same
        # citations, so what it says and what's cited can't drift apart.
        for r in keep:
            citation = {"source": r["source"]}
            if citation not in self.turn.citations:
                self.turn.citations.append(citation)

        trimmed = [
            {"text": r["text"][:MAX_PASSAGE_CHARS], "source": r["source"]}
            for r in keep
        ]
        return json.dumps({"results": trimmed})

    def _run_text2sql(self, question: str) -> str:
        result = self._text2sql.answer_question(question, self._guest["Guest_ID"])
        timings = result.pop("timings_ms", None)
        if timings and self.metrics is not None:
            self.metrics.add_stage("sql_generation", timings["sql_generation"])
            self.metrics.add_stage("db_execution", timings["db_execution"])
        if "error" not in result:
            self.turn.data_table = {"question": question, **result}
        return json.dumps(result)

    def _reject_oversized_party(self, name: str, args: dict) -> Optional[str]:
        """Reject party sizes over the 8-guest maximum before staging."""
        party_size = args.get("party_size")
        if not isinstance(party_size, int) or party_size <= MAX_PARTY_SIZE:
            return None
        log.info("party_size_rejected tool=%s size=%s", name, party_size)
        return json.dumps({
            "error": {
                "code": "party_size_exceeds_maximum",
                "message": (
                    f"A single reservation seats at most {MAX_PARTY_SIZE} "
                    f"guests; {party_size} was requested. Do not stage this "
                    f"booking. Instead, propose splitting the group into "
                    f"multiple reservations of {MAX_PARTY_SIZE} guests or "
                    f"fewer each (e.g. {party_size} guests could be two "
                    f"tables), and offer to book each one — asking the "
                    f"guest to confirm each separately."
                ),
            }
        })

    def _has_date_signal(self) -> bool:
        """True if the guest stated a date recently (day name, tonight/
        tomorrow, month, numeric date, or an ordinal like \"the 8th\")."""
        return any(DATE_SIGNAL_PATTERN.search(m)
                  for m in self._recent_guest_messages)

    @staticmethod
    def _resolve_venue(venue_ref: str, restaurants: dict) -> Optional[dict]:
        """Name-or-ID venue resolution via the shared resolver in api.store."""
        from api.store import resolve_venue_ref
        return resolve_venue_ref(venue_ref)

    def _conflict_at(self, reservation_date: Optional[str],
                     reservation_time: Optional[str]) -> Optional[dict]:
        """The guest's Confirmed reservation at this exact date+time, or
        None. Runs on every create attempt as the conflict safety net."""
        if not reservation_date or not reservation_time:
            return None
        from data.database import get_readonly_conn
        conn = get_readonly_conn()
        try:
            row = conn.execute(
                "SELECT * FROM reservations WHERE guest_id = ? AND "
                "guest_name = ? AND reservation_date = ? AND "
                "reservation_time = ? AND status = 'Confirmed' LIMIT 1",
                (self._guest["Guest_ID"],
                 f"{self._guest['First_Name']} {self._guest['Last_Name']}",
                 reservation_date, reservation_time)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def _precheck_target(self, name: str,
                         args: dict) -> tuple[Optional[str], Optional[dict]]:
        """Validate the target before a card is staged: venue resolution
        for creates; existence, ownership (id AND name), and not-cancelled
        for modify/cancel. Returns (error_json, context)."""
        if name == "create_reservation":
            from api.store import RESTAURANTS

            # Merge each attempt into the per-session booking draft so a
            # follow-up answer like "tomorrow" completes the booking.
            # additional_table is a routing flag, never a booking field.
            additional_table = bool(args.pop("additional_table", False))
            venue = None
            if args.get("venue_id"):
                venue = self._resolve_venue(args["venue_id"], RESTAURANTS)
                if venue is None:
                    return json.dumps({"error": {
                        "code": "unknown_venue",
                        "message": (f"'{args.get('venue_id')}' doesn't match "
                                    f"any venue. Valid venues: "
                                    + ", ".join(f"{v['name']} ({vid})"
                                                for vid, v in RESTAURANTS.items())
                                    + ". Pass the venue name the guest used, "
                                      "or its exact ID.")}}), None
                # Different venue means a different booking: reset.
                if self._booking_draft.get("venue_id") not in (None, venue["venue_id"]):
                    self._booking_draft = {}
                self._booking_draft["venue_id"] = venue["venue_id"]

            # The date slot only accepts a guest-stated date; anything
            # else is an invented default and is dropped.
            if args.get("reservation_date"):
                if self._has_date_signal():
                    self._booking_draft["reservation_date"] = args["reservation_date"]
                else:
                    log.info("create_draft_date_rejected reason=not_guest_stated "
                             "date=%s", args["reservation_date"])
            if args.get("reservation_time"):
                self._booking_draft["reservation_time"] = args["reservation_time"]
            if args.get("party_size") is not None:
                self._booking_draft["party_size"] = args["party_size"]
            if args.get("special_requests"):
                self._booking_draft["special_requests"] = args["special_requests"]
            if args.get("dietary_notes"):
                self._booking_draft["dietary_notes"] = args["dietary_notes"]

            # All four fields must exist before staging.
            missing = [f for f in ("venue_id", "reservation_date",
                                   "reservation_time", "party_size")
                       if not self._booking_draft.get(f)]
            if "reservation_date" in missing:
                log.info("create_reservation_blocked reason=no_guest_date")
                return json.dumps({"error": {
                    "code": "date_not_specified",
                    "message": "The guest has not stated a date."}}), None
            if missing:
                pretty = {"venue_id": "the restaurant",
                          "reservation_time": "the time",
                          "party_size": "the party size"}
                need = ", ".join(pretty[f] for f in missing)
                return json.dumps({"error": {
                    "code": "booking_details_missing",
                    "message": (f"Missing before this can be staged: {need}. "
                                f"Ask the guest for exactly that — do not "
                                f"invent or assume values.")}}), None

            args.clear()
            args.update(self._booking_draft)
            venue = RESTAURANTS[args["venue_id"]]

            conflict = self._conflict_at(args["reservation_date"],
                                         args["reservation_time"])
            if conflict and (additional_table
                             and conflict["venue_id"] == args["venue_id"]):
                # A deliberate extra table for the same party at the same
                # venue (e.g. a split for 10+). Still staged for explicit
                # guest confirmation like any other booking.
                log.info("create_reservation_additional_table venue=%s",
                         args["venue_id"])
                conflict = None
            if conflict:
                log.info("create_reservation_blocked reason=conflict")
                return json.dumps({"error": {
                    "code": "reservation_conflict",
                    "message": (
                        f"The guest ALREADY has a Confirmed reservation at "
                        f"{conflict['venue_name']} on "
                        f"{conflict['reservation_date']} at "
                        f"{conflict['reservation_time']} (party of "
                        f"{conflict['party_size']}, confirmation "
                        f"{conflict['confirmation_number']}). Do not stage "
                        f"this booking. Tell the guest about the existing "
                        f"reservation and ask how they'd like to proceed — "
                        f"pick a different time, or cancel/modify the "
                        f"existing one first. Only if the guest wants an "
                        f"ADDITIONAL table for the same party at this same "
                        f"venue and time (e.g. a split for a large group), "
                        f"call create_reservation again with "
                        f"additional_table=true."
                    ),
                }}), None
            return None, venue

        if name not in {"modify_reservation", "cancel_reservation"}:
            return None, None
        reservation_id = args.get("reservation_id")
        if not reservation_id:
            return json.dumps({"error": {
                "code": "missing_reservation_id",
                "message": "A reservation_id is required. Use "
                           "list_reservations to find the right one."}}), None

        from data.database import get_readonly_conn
        conn = get_readonly_conn()
        try:
            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (reservation_id,)).fetchone()
        finally:
            conn.close()

        guest_name = f"{self._guest['First_Name']} {self._guest['Last_Name']}"
        if (row is None or row["guest_id"] != self._guest["Guest_ID"]
                or row["guest_name"] != guest_name):
            return json.dumps({"error": {
                "code": "reservation_not_found",
                "message": (
                    f"No reservation '{reservation_id}' was found on this "
                    f"guest's account. Do not state this as flat fact — "
                    f"gently tell the guest, in one warm sentence, that you "
                    f"don't see it and that Guest Services (Deck 5) can "
                    f"take a closer look if they believe it should be "
                    f"there, then offer the natural next step. Also call "
                    f"list_reservations fresh to get their current, correct "
                    f"reservation_ids — never reuse an ID from earlier in "
                    f"the conversation without re-checking it.")}}), None

        if row["status"] == "Cancelled":
            verb = ("cancel" if name == "cancel_reservation" else "modify")
            return json.dumps({"error": {
                "code": "already_cancelled",
                "message": (f"That reservation ({row['venue_name']} on "
                            f"{row['reservation_date']} at "
                            f"{row['reservation_time']}) is ALREADY "
                            f"cancelled — do not stage anything. Tell the "
                            f"guest plainly that it was already cancelled"
                            + (", so there is nothing further to cancel."
                               if verb == "cancel" else
                               "; offer to create a new reservation "
                               "instead if they'd like."))}}), None

        return None, dict(row)

    def _stage_pending(self, name: str, args: dict,
                       target: Optional[dict] = None) -> str:
        """Stage one action for guest confirmation; nothing executes here.
        Only one action may be pending at a time."""
        if self._pending:
            return json.dumps({
                "status": "another_action_pending",
                "instruction": ("An action is already awaiting the guest's "
                                "confirmation. Present THAT one and wait "
                                "for the guest to confirm or decline it. "
                                "Stage this next action only after the "
                                "current one is resolved — never tell the "
                                "guest that several actions are staged at "
                                "once."),
            })
        action_id = f"act_{secrets.token_hex(4)}"
        self._pending[action_id] = (name, args)
        if name == "create_reservation":
            self._booking_draft = {}  # staged action owns the values now
        display_args = {k: v for k, v in args.items()
                        if k not in {"guest_id", "guest_name"} and v is not None}
        if target and name == "create_reservation":
            display_args = {
                "venue": target["name"],
                **{k: v for k, v in display_args.items() if k != "venue_id"},
            }
        elif target:
            display_args = {
                "venue": target["venue_name"],
                "current_booking": (f"{target['reservation_date']} at "
                                    f"{target['reservation_time']}, party of "
                                    f"{target['party_size']}"),
                **{k: v for k, v in display_args.items()
                   if k != "reservation_id"},
            }
        self.turn.pending_action = {
            "action_id": action_id,
            "tool": name,
            "arguments": display_args,
        }
        log.info("pending_action id=%s tool=%s args=%s",
                 action_id, name, json.dumps(redact_args(display_args)))
        return json.dumps({
            "status": "pending_confirmation",
            "action_id": action_id,
            "details": display_args,
            "instruction": ("Present these exact details to the guest and ask "
                            "them to confirm. The system executes only after "
                            "the guest explicitly confirms."),
        })

    async def run_pending(self, action_id: str) -> str:
        """Execute a stored action with its exact stored arguments. Called
        only from an explicit guest confirmation, never by the model."""
        if action_id not in self._pending:
            return json.dumps({"error": {
                "code": "unknown_action",
                "message": "There is no pending action to confirm."}})
        name, args = self._pending.pop(action_id)
        log.info("confirmed_action id=%s tool=%s", action_id, name)
        return await self._call_mcp(name, dict(args))

    async def _call_mcp(self, name: str, args: dict) -> str:
        if name in GUEST_SCOPED:
            args["guest_id"] = self._guest["Guest_ID"]
        if name == "create_reservation":
            args["guest_name"] = (f"{self._guest['First_Name']} "
                                  f"{self._guest['Last_Name']}")
            args.setdefault("cabin_number", self._guest["Cabin_Number"])

        result = await self._mcp.call_tool(name, args)
        payload = "".join(block.text for block in result.content
                          if getattr(block, "text", None))
        if not payload:
            return json.dumps({"error": {
                "code": "empty_result", "message": "The tool returned nothing."}})

        self._capture_reservation_card(name, payload)
        log.info("tool_result name=%s ok", name)
        return payload

    def _capture_reservation_card(self, name: str, payload: str) -> None:
        if name not in STATE_CHANGING:
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and "reservation_id" in data:
            self.turn.reservation_card = {
                key: data.get(key) for key in (
                    "reservation_id", "venue_name", "reservation_date",
                    "reservation_time", "party_size", "status",
                    "confirmation_number", "special_requests", "dietary_notes")
            }
