"""Offline guardrail scenarios: multi-step booking flows through the
real executor and store, asserting guard behavior AND database outcome."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from api import store
from assistant.guests import get_guest
from assistant.tools import DATE_QUESTION, MAX_PARTY_SIZE, ToolExecutor


class StoreBackedExecutor(ToolExecutor):
    """ToolExecutor with MCP calls routed straight to the store layer;
    identity injection mirrors the production executor."""

    async def _call_mcp(self, name: str, args: dict) -> str:
        args = {k: v for k, v in args.items() if v is not None}
        args["guest_id"] = self._guest["Guest_ID"]
        if name == "create_reservation":
            args["guest_name"] = (f"{self._guest['First_Name']} "
                                  f"{self._guest['Last_Name']}")
            args.setdefault("cabin_number", self._guest["Cabin_Number"])
        try:
            if name == "create_reservation":
                return json.dumps(store.create_reservation(**args))
            if name == "modify_reservation":
                rid = args.pop("reservation_id")
                gid = args.pop("guest_id", None)
                args.pop("guest_name", None)
                return json.dumps(store.modify_reservation(
                    rid, acting_guest_id=gid, **args))
            if name == "cancel_reservation":
                return json.dumps(store.cancel_reservation(
                    args["reservation_id"],
                    acting_guest_id=args.get("guest_id")))
            if name == "list_reservations":
                rows = store.list_reservations(
                    guest_id=args.get("guest_id"), status=args.get("status"),
                    from_date=args.get("from_date"), to_date=args.get("to_date"),
                    venue_id=args.get("venue_id"))
                return json.dumps({"reservations": rows, "count": len(rows)})
            raise AssertionError(f"unmapped tool {name}")
        except store.StoreError as err:
            return json.dumps({"error": {"code": err.code, "message": str(err)}})


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh seeded database per test; DB_PATH is resolved at call time,
    so patching it redirects every layer."""
    import config
    import data.database as db
    test_db = tmp_path / "ocean_test.db"
    monkeypatch.setattr(config, "DB_PATH", test_db)
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_database(db_path=test_db)
    yield


@pytest.fixture()
def executor():
    guest = get_guest("G100036")  # Barbara Martinez — no seed reservations
    ex = StoreBackedExecutor(mcp_session=None, retriever=None,
                             text2sql=None, guest=guest)
    ex.reset_turn()
    return ex


def _guest_rows(guest_id="G100036", **kw):
    return store.list_reservations(guest_id=guest_id, **kw)


async def _book(ex, said, **args):
    ex.reset_turn()
    ex.set_recent_guest_messages([said])
    return json.loads(await ex.execute("create_reservation", args))


# ── Booking-draft and date guardrails ──────────────────────────────────────

@pytest.mark.asyncio
async def test_booking_without_guest_date_is_blocked_and_nothing_written(executor):
    out = await _book(executor, "book sakura at 8pm for 2",
                      venue_id="Sakura", reservation_date="2026-02-07",
                      reservation_time="20:00", party_size=2)
    assert out["error"]["code"] == "date_not_specified"
    assert executor.turn.pending_action is None
    assert _guest_rows() == []


@pytest.mark.asyncio
async def test_invented_date_is_rejected_but_other_fields_are_drafted(executor):
    await _book(executor, "book sakura at 8pm for 2",
                venue_id="Sakura", reservation_date="2026-02-07",
                reservation_time="20:00", party_size=2)
    assert "reservation_date" not in executor._booking_draft
    assert executor._booking_draft["venue_id"] == "DIN004"
    assert executor._booking_draft["party_size"] == 2


@pytest.mark.asyncio
async def test_draft_completes_booking_when_guest_supplies_the_date(executor):
    await _book(executor, "book sakura at 8pm for 2",
                venue_id="Sakura", reservation_date="2026-02-07",
                reservation_time="20:00", party_size=2)
    executor.reset_turn()
    executor.set_recent_guest_messages(["book sakura at 8pm for 2", "tomorrow"])
    out = json.loads(await executor.execute(
        "create_reservation", {"reservation_date": "2026-02-08"}))
    assert out["status"] == "pending_confirmation"
    card = executor.turn.pending_action["arguments"]
    assert card["venue"] == "Sakura"
    assert card["reservation_date"] == "2026-02-08"
    assert card["party_size"] == 2
    result = json.loads(await executor.run_pending(out["action_id"]))
    assert result["status"] == "Confirmed"
    rows = _guest_rows()
    assert len(rows) == 1 and rows[0]["venue_id"] == "DIN004"


@pytest.mark.asyncio
async def test_date_question_constant_matches_agent_contract():
    assert DATE_QUESTION.startswith("What date would you like")


# ── Party-size guardrails ──────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("size", [6, 7, MAX_PARTY_SIZE])
async def test_party_sizes_up_to_max_stage_normally(executor, size):
    out = await _book(executor, f"book sakura tonight at 7pm for {size}",
                      venue_id="Sakura", reservation_date="2026-02-07",
                      reservation_time="19:00", party_size=size)
    assert out["status"] == "pending_confirmation"
    executor.clear_pending()


@pytest.mark.asyncio
async def test_party_above_max_is_rejected_before_any_card(executor):
    out = await _book(executor, "book sakura tonight at 7pm for 9",
                      venue_id="Sakura", reservation_date="2026-02-07",
                      reservation_time="19:00", party_size=9)
    assert out["error"]["code"] == "party_size_exceeds_maximum"
    assert executor.turn.pending_action is None
    assert _guest_rows() == []


# ── Conflict guardrail ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_double_booking_same_time_blocked_at_staging(executor):
    out = await _book(executor, "book steakhouse tomorrow at 7pm for 2",
                      venue_id="The Steakhouse", reservation_date="2026-02-08",
                      reservation_time="19:00", party_size=2)
    json.loads(await executor.run_pending(out["action_id"]))

    out2 = await _book(executor, "book sakura tomorrow at 7pm for 2",
                       venue_id="Sakura", reservation_date="2026-02-08",
                       reservation_time="19:00", party_size=2)
    assert out2["error"]["code"] == "reservation_conflict"
    assert "The Steakhouse" in out2["error"]["message"]
    assert len(_guest_rows()) == 1

    out3 = await _book(executor, "book sakura tomorrow at 8:30pm for 2",
                       venue_id="Sakura", reservation_date="2026-02-08",
                       reservation_time="20:30", party_size=2)
    assert out3["status"] == "pending_confirmation"
    executor.clear_pending()


# ── Cancel / modify integrity ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_twice_second_attempt_blocked_with_no_card(executor):
    out = await _book(executor, "book sakura tomorrow at 7pm for 2",
                      venue_id="Sakura", reservation_date="2026-02-08",
                      reservation_time="19:00", party_size=2)
    created = json.loads(await executor.run_pending(out["action_id"]))
    rid = created["reservation_id"]

    executor.reset_turn()
    out2 = json.loads(await executor.execute(
        "cancel_reservation", {"reservation_id": rid}))
    result = json.loads(await executor.run_pending(out2["action_id"]))
    assert result["status"] == "Cancelled"

    executor.reset_turn()
    again = json.loads(await executor.execute(
        "cancel_reservation", {"reservation_id": rid}))
    assert again["error"]["code"] == "already_cancelled"
    assert executor.turn.pending_action is None
    cancelled = _guest_rows(status="Cancelled")
    assert len(cancelled) == 1


@pytest.mark.asyncio
async def test_unknown_reservation_id_blocked(executor):
    executor.reset_turn()
    out = json.loads(await executor.execute(
        "modify_reservation", {"reservation_id": "DRES999999", "party_size": 4}))
    assert out["error"]["code"] == "reservation_not_found"
    assert executor.turn.pending_action is None


@pytest.mark.asyncio
async def test_name_mismatched_seed_row_is_invisible_to_its_guest_id():
    michael = get_guest("G100005")
    rows = store.list_reservations(guest_id="G100005")
    assert all(r["reservation_id"] != "DRES000002" for r in rows)
    assert all(r["guest_name"] == "Michael Williams" for r in rows)
    assert michael is not None


# ── Single-pending discipline ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_staging_refused_while_one_pending(executor):
    out = await _book(executor, "book le bistro tomorrow at 6pm for 2",
                      venue_id="Le Bistro", reservation_date="2026-02-08",
                      reservation_time="18:00", party_size=2)
    assert out["status"] == "pending_confirmation"
    second = json.loads(await executor.execute("create_reservation", {
        "venue_id": "Sakura", "reservation_date": "2026-02-08",
        "reservation_time": "19:00", "party_size": 2}))
    assert second["status"] == "another_action_pending"
    result = json.loads(await executor.run_pending(out["action_id"]))
    assert result["venue_id"] == "DIN003"


# ── Venue resolution ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("ref,canonical", [
    ("The Steakhouse", "DIN001"), ("steakhouse", "DIN001"),
    ("chefs table", "DIN005"), ("SAKURA", "DIN004"), ("DIN002", "DIN002"),
])
async def test_venue_names_resolve_deterministically(executor, ref, canonical):
    out = await _book(executor, f"book {ref} tomorrow at 6pm for 2",
                      venue_id=ref, reservation_date="2026-02-08",
                      reservation_time="18:00", party_size=2)
    assert out["status"] == "pending_confirmation"
    _, (_, staged_args) = executor._pending.popitem()
    assert staged_args["venue_id"] == canonical
    executor.clear_pending()


@pytest.mark.asyncio
async def test_ambiguous_venue_name_returns_clarify_error(executor):
    executor.reset_turn()
    executor.set_recent_guest_messages(["show my la bistro reservations"])
    out = json.loads(await executor.execute(
        "list_reservations", {"venue_id": "la bistro"}))
    assert out["error"]["code"] == "unknown_venue"


# ── Long-list structured table ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_long_reservation_list_attaches_complete_table():
    michael = get_guest("G100005")
    ex = StoreBackedExecutor(mcp_session=None, retriever=None,
                             text2sql=None, guest=michael)
    ex.reset_turn()
    ex.set_recent_guest_messages(["show all my sakura reservations"])
    out = json.loads(await ex.execute(
        "list_reservations", {"venue_id": "Sakura"}))
    assert out["count"] >= 10
    assert "note" in out and "complete table" in out["note"]
    table = ex.turn.data_table
    assert table is not None
    assert table["row_count"] == out["count"]
    assert len(table["rows"]) == out["count"]
    assert table["columns"][0] == "venue"

@pytest.mark.asyncio
async def test_additional_table_allows_same_venue_split(executor):
    out = await _book(executor, "book sakura tomorrow at 7pm, we are 14 so split us",
                      venue_id="Sakura", reservation_date="2026-02-08",
                      reservation_time="19:00", party_size=7)
    first = json.loads(await executor.run_pending(out["action_id"]))
    assert first["status"] == "Confirmed"

    # Second table at the identical slot: blocked without the flag...
    blocked = await _book(executor, "and a second table tomorrow for the other 7",
                          venue_id="Sakura", reservation_date="2026-02-08",
                          reservation_time="19:00", party_size=7)
    assert blocked["error"]["code"] == "reservation_conflict"

    # ...and staged normally with it.
    out2 = await _book(executor, "yes book the second table tomorrow for the other 7",
                       venue_id="Sakura", reservation_date="2026-02-08",
                       reservation_time="19:00", party_size=7,
                       additional_table=True)
    assert out2["status"] == "pending_confirmation"
    second = json.loads(await executor.run_pending(out2["action_id"]))
    assert second["status"] == "Confirmed"

    rows = _guest_rows(venue_id="DIN004")
    assert len(rows) == 2
    assert sum(r["party_size"] for r in rows) == 14


@pytest.mark.asyncio
async def test_additional_table_does_not_bypass_other_venue_conflict(executor):
    out = await _book(executor, "book sakura tomorrow at 7pm for 2",
                      venue_id="Sakura", reservation_date="2026-02-08",
                      reservation_time="19:00", party_size=2)
    json.loads(await executor.run_pending(out["action_id"]))

    # A different venue at the same time is a real conflict, flag or not.
    blocked = await _book(executor, "also book le bistro tomorrow at 7pm for 2",
                          venue_id="Le Bistro", reservation_date="2026-02-08",
                          reservation_time="19:00", party_size=2,
                          additional_table=True)
    assert blocked["error"]["code"] == "reservation_conflict"
    assert len(_guest_rows()) == 1
