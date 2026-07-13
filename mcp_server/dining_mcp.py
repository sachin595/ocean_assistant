"""Dining Reservation MCP server — exposes the REST API as tools for
the assistant. Tool docstrings are model-facing descriptions."""

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from mcp.server.fastmcp import FastMCP

from config import DINING_API_BASE_URL

mcp = FastMCP(
    "Ocean Cruises Dining Reservations",
    instructions=(
        "Specialty dining reservation tools for Ocean Cruises. "
        "Always confirm with the guest before creating, modifying, or "
        "cancelling a reservation."
    ),
)

_client = httpx.Client(base_url=DINING_API_BASE_URL, timeout=10.0)


def _call(method: str, path: str, **kwargs) -> dict:
    """HTTP call with errors normalized into a dict the LLM can read."""
    try:
        resp = _client.request(method, path, **kwargs)
        body = resp.json()
        if resp.is_success:
            return body
        # Structured API error ({"error": {...}}) or FastAPI validation detail.
        if "error" in body:
            return body
        return {"error": {"code": "validation_error", "message": str(body.get("detail", body))}}
    except httpx.ConnectError:
        return {"error": {"code": "service_unavailable",
                          "message": "The dining reservation service is not reachable."}}
    except Exception as exc:  # noqa: BLE001 — surface anything to the LLM, never crash
        return {"error": {"code": "unexpected_error", "message": str(exc)}}


@mcp.tool()
def list_restaurants() -> dict:
    """List all five specialty dining restaurants with cuisine, cover charge
    per person, operating hours, deck location, and seating capacity.

    Use this when the guest asks what specialty restaurants exist, their
    prices, or where they are. (For menus/dress code details, use the
    knowledge base instead.)
    """
    return _call("GET", "/api/v1/restaurants")


@mcp.tool()
def check_availability(venue_id: str, date: str) -> dict:
    """Check seating availability for one restaurant on one date.

    Args:
        venue_id: DIN001=The Steakhouse, DIN002=La Trattoria,
                  DIN003=Le Bistro, DIN004=Sakura, DIN005=Chef's Table.
        date: YYYY-MM-DD (e.g. '2026-02-08').

    Returns total capacity, covers already booked, covers remaining, and a
    per-time-slot breakdown. Call this BEFORE creating a reservation.
    """
    return _call("GET", f"/api/v1/restaurants/{venue_id}/availability",
                 params={"date": date})


@mcp.tool()
def list_reservations(guest_id: Optional[str] = None,
                      status: Optional[str] = None,
                      from_date: Optional[str] = None,
                      to_date: Optional[str] = None,
                      venue_id: Optional[str] = None) -> dict:
    """List dining reservations, filterable by guest, status, date range,
    and venue. This tool is for showing reservation DETAILS. Do NOT use it
    to answer counting questions ("how many reservations", totals,
    per-restaurant breakdowns) — use query_guest_data for those, which
    computes exact counts in SQL instead of relying on manual counting.

    Args:
        guest_id: e.g. 'G100005'. Almost always pass the current guest's ID.
        status: 'Confirmed' or 'Cancelled'. Default: all.
        from_date / to_date: YYYY-MM-DD inclusive bounds. IMPORTANT — some
            guests have dozens of reservations, including some MONTHS in
            the future beyond the current sailing; the data is never
            limited to "this cruise's" dates. When the guest asks about
            "my reservations" or "upcoming dinners", pass from_date=today.
            When the guest names a specific month or date (e.g. "in May"),
            pass matching from_date/to_date for that period and trust
            whatever comes back — never assume reservations can't exist
            outside the current week.
        venue_id: accepts the restaurant NAME as the guest said it
            ("Sakura", "the steakhouse") or an exact ID (DIN001–DIN005);
            either is resolved deterministically by the system. When the
            guest asks about a SPECIFIC restaurant ("show my Sakura
            reservations", "do I have anything at Chef's Table"), ALWAYS
            pass this instead of guessing a date range — a guessed
            window can miss reservations outside it, and different guesses
            across calls can silently return different, incomplete results
            for the exact same question. venue_id alone (with no date
            bounds) returns every reservation at that venue, guaranteed.
    """
    params = {k: v for k, v in {
        "guest_id": guest_id, "status": status,
        "from_date": from_date, "to_date": to_date,
        "venue_id": venue_id}.items() if v}
    return _call("GET", "/api/v1/reservations", params=params)


@mcp.tool()
def check_reservation_conflict(guest_id: str, reservation_date: str,
                               reservation_time: str) -> dict:
    """Check whether the guest ALREADY has an active reservation at the
    same date and time (at any venue), BEFORE booking a new one. Always
    call this before create_reservation.

    Args:
        guest_id: e.g. 'G100005' — the current guest's ID.
        reservation_date: YYYY-MM-DD (e.g. '2026-02-08').
        reservation_time: HH:MM 24-hour (e.g. '19:00').

    Returns:
        conflict: true/false.
        existing_reservation: the conflicting reservation's details, if any.
        message: a plain-English summary, e.g. "The guest already has a
            Confirmed reservation at Le Bistro on 2026-02-08 at 19:00...".
        If conflict is true, tell the guest about the existing booking and
        ask how they'd like to proceed (keep both, change the time, or
        replace the old one) — do not silently double-book them.
    """
    return _call("GET", "/api/v1/reservations/conflict", params={
        "guest_id": guest_id, "reservation_date": reservation_date,
        "reservation_time": reservation_time})


@mcp.tool()
def create_reservation(guest_id: str, guest_name: str, venue_id: str,
                       reservation_date: str, reservation_time: str,
                       party_size: int, cabin_number: Optional[str] = None,
                       special_requests: Optional[str] = None,
                       dietary_notes: Optional[str] = None,
                       additional_table: bool = False) -> dict:
    """Create a new specialty dining reservation.

    ONLY call this AFTER the guest has explicitly confirmed the exact
    details (venue, date, time, party size).

    Args:
        guest_id: The current guest's ID (e.g. 'G100005').
        guest_name: The guest's full name from their profile.
        venue_id: pass the venue NAME exactly as the guest said it (e.g.
            "Sakura", "The Steakhouse", "Chef's Table") — the system
            resolves it to the correct venue. An exact ID (DIN001–DIN005)
            also works, but never translate a name to an ID yourself.
        reservation_date: YYYY-MM-DD.
        reservation_time: HH:MM 24-hour, within venue hours
            (17:30–21:30 for most venues; Chef's Table 18:00–21:00).
        party_size: 1–8 guests.
        special_requests: e.g. 'Anniversary dinner', 'Window table'.
        dietary_notes: e.g. 'Gluten-Free', 'Shellfish allergy'.
        additional_table: pass true ONLY when deliberately booking an
            extra table for the same party at the same venue and time —
            e.g. the second table of a split for a group larger than 8.
            Without it, a second booking at the same date and time is
            rejected as an accidental double-booking.

    Returns the confirmed reservation including its confirmation number, or
    an error (e.g. no_capacity) explaining why it could not be made.
    """
    payload = {
        "guest_id": guest_id, "guest_name": guest_name, "venue_id": venue_id,
        "reservation_date": reservation_date, "reservation_time": reservation_time,
        "party_size": party_size, "cabin_number": cabin_number,
        "special_requests": special_requests, "dietary_notes": dietary_notes,
    }
    return _call("POST", "/api/v1/reservations", json=payload)


@mcp.tool()
def modify_reservation(reservation_id: str, guest_id: str,
                       reservation_date: Optional[str] = None,
                       reservation_time: Optional[str] = None,
                       party_size: Optional[int] = None,
                       special_requests: Optional[str] = None,
                       dietary_notes: Optional[str] = None) -> dict:
    """Modify an existing reservation. Pass ONLY the fields that change.

    ONLY call this AFTER the guest has explicitly confirmed the change.
    The guest may only modify their own reservations — pass the current
    guest's ID so ownership is verified.

    Args:
        reservation_id: e.g. 'DRES000002'.
        guest_id: The current guest's ID (ownership check).
        reservation_date: New date, YYYY-MM-DD.
        reservation_time: New time, HH:MM 24-hour.
        party_size: New size, 1–8.
    """
    changes = {k: v for k, v in {
        "reservation_date": reservation_date, "reservation_time": reservation_time,
        "party_size": party_size, "special_requests": special_requests,
        "dietary_notes": dietary_notes}.items() if v is not None}
    return _call("PATCH", f"/api/v1/reservations/{reservation_id}",
                 params={"guest_id": guest_id}, json=changes)


@mcp.tool()
def cancel_reservation(reservation_id: str, guest_id: str,
                       reason: Optional[str] = None) -> dict:
    """Cancel a reservation (soft cancel — the record is kept with
    status 'Cancelled').

    ONLY call this AFTER the guest has explicitly confirmed the
    cancellation. The guest may only cancel their own reservations.

    Args:
        reservation_id: e.g. 'DRES000002'.
        guest_id: The current guest's ID (ownership check).
        reason: Optional short reason, e.g. 'Guest changed plans'.
    """
    return _call("DELETE", f"/api/v1/reservations/{reservation_id}",
                 params={"guest_id": guest_id},
                 json={"reason": reason} if reason else None)


if __name__ == "__main__":
    mcp.run()  # stdio transport
