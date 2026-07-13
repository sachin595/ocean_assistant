"""Reservation store: SQLite persistence and business rules for the
dining REST API (validation, capacity, ownership, soft cancel)."""

import random
import sqlite3
import string
from datetime import datetime, time as dtime
from typing import Optional

from data.database import get_conn, init_database

# ── Static restaurant catalog (from the reference data) ──
RESTAURANTS = {
    "DIN001": {"venue_id": "DIN001", "name": "The Steakhouse", "cuisine": "Premium steaks & seafood",
               "cover_charge": 65.00, "hours": "17:30-21:30", "deck": 10, "capacity": 60},
    "DIN002": {"venue_id": "DIN002", "name": "La Trattoria", "cuisine": "Italian multi-course tasting",
               "cover_charge": 45.00, "hours": "17:30-21:30", "deck": 10, "capacity": 80},
    "DIN003": {"venue_id": "DIN003", "name": "Le Bistro", "cuisine": "French bistro",
               "cover_charge": 55.00, "hours": "17:30-21:30", "deck": 11, "capacity": 50},
    "DIN004": {"venue_id": "DIN004", "name": "Sakura", "cuisine": "Japanese sushi & teppanyaki",
               "cover_charge": 50.00, "hours": "17:30-21:30", "deck": 11, "capacity": 45},
    "DIN005": {"venue_id": "DIN005", "name": "Chef's Table", "cuisine": "Multi-course chef's tasting",
               "cover_charge": 95.00, "hours": "18:00-21:00", "deck": 12, "capacity": 24},
}

MIN_PARTY, MAX_PARTY = 1, 8
SLOT_MINUTES = 30  # availability is reported in 30-minute seatings


class StoreError(Exception):
    """Business-rule violation. `code` maps to an HTTP status in the API layer."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ── Initialization ─────────────────────────────────────────────────────────

import re


def resolve_venue_ref(ref: str) -> Optional[dict]:
    """Resolve a venue by exact ID or by name in any casing/punctuation.
    Deterministic in Python — never left to the model's memory."""
    candidate = (ref or "").strip()
    if candidate in RESTAURANTS:
        return RESTAURANTS[candidate]
    normalized = re.sub(r"[^a-z0-9 ]", "", candidate.lower()).strip()
    normalized = re.sub(r"^the ", "", normalized)
    for venue in RESTAURANTS.values():
        name = re.sub(r"[^a-z0-9 ]", "", venue["name"].lower()).strip()
        name = re.sub(r"^the ", "", name)
        if normalized == name:
            return venue
    return None


def init_db() -> int:
    """Create schema and seed all tables (each only when empty).

    Returns the number of reservation rows seeded (0 on a warm restart).
    """
    return init_database()["reservations"]


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_reservation_id(conn: sqlite3.Connection) -> str:
    (max_id,) = conn.execute(
        "SELECT MAX(CAST(SUBSTR(reservation_id, 5) AS INTEGER)) FROM reservations "
        "WHERE reservation_id LIKE 'DRES%'"
    ).fetchone()
    return f"DRES{(max_id or 0) + 1:06d}"


def _new_confirmation_number() -> str:
    return "DIN-" + "".join(random.choices(string.digits, k=5))


def _parse_hours(hours: str) -> tuple[dtime, dtime]:
    start, end = hours.split("-")
    return (dtime.fromisoformat(start), dtime.fromisoformat(end))


def _validate_venue(venue_id: str) -> dict:
    venue = resolve_venue_ref(venue_id)
    if not venue:
        raise StoreError(
            "venue_not_found",
            f"Unknown venue '{venue_id}'. Valid venues: "
            + ", ".join(f"{v['name']} ({vid})"
                        for vid, v in RESTAURANTS.items()) + ".",
        )
    return venue


def _validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise StoreError("invalid_date", f"Date '{value}' must be YYYY-MM-DD.")
    return value


def _validate_time(value: str, venue: dict) -> str:
    try:
        t = dtime.fromisoformat(value)
    except ValueError:
        raise StoreError("invalid_time", f"Time '{value}' must be HH:MM (24-hour).")
    start, end = _parse_hours(venue["hours"])
    if not (start <= t <= end):
        raise StoreError(
            "outside_hours",
            f"{venue['name']} seats guests between {venue['hours']}.",
        )
    return value


def _validate_party_size(size: int) -> int:
    if not (MIN_PARTY <= size <= MAX_PARTY):
        raise StoreError(
            "invalid_party_size",
            f"Party size must be between {MIN_PARTY} and {MAX_PARTY}.",
        )
    return size


def _booked_covers(conn: sqlite3.Connection, venue_id: str, date: str,
                   exclude_id: Optional[str] = None) -> int:
    """Total confirmed covers for a venue on a date (optionally excluding one
    reservation — used when modifying, so a reservation doesn't count against
    itself)."""
    sql = (
        "SELECT COALESCE(SUM(party_size), 0) FROM reservations "
        "WHERE venue_id = ? AND reservation_date = ? AND status = 'Confirmed'"
    )
    params: list = [venue_id, date]
    if exclude_id:
        sql += " AND reservation_id != ?"
        params.append(exclude_id)
    (total,) = conn.execute(sql, params).fetchone()
    return total


def _check_capacity(conn: sqlite3.Connection, venue: dict, date: str,
                    party_size: int, exclude_id: Optional[str] = None) -> None:
    booked = _booked_covers(conn, venue["venue_id"], date, exclude_id)
    remaining = venue["capacity"] - booked
    if party_size > remaining:
        raise StoreError(
            "no_capacity",
            f"{venue['name']} has only {max(remaining, 0)} seats left on {date} "
            f"(capacity {venue['capacity']}).",
        )


# ── Public operations (called by the FastAPI routes) ──────────────────────

def list_restaurants() -> list[dict]:
    return list(RESTAURANTS.values())


def get_availability(venue_id: str, date: str) -> dict:
    venue = _validate_venue(venue_id)
    _validate_date(date)
    conn = get_conn()
    try:
        booked = _booked_covers(conn, venue_id, date)
        remaining = max(venue["capacity"] - booked, 0)

        # Per-slot picture: covers booked at each 30-minute seating.
        rows = conn.execute(
            "SELECT reservation_time, SUM(party_size) AS covers FROM reservations "
            "WHERE venue_id = ? AND reservation_date = ? AND status = 'Confirmed' "
            "GROUP BY reservation_time",
            (venue_id, date),
        ).fetchall()
        by_slot = {r["reservation_time"]: r["covers"] for r in rows}

        start, end = _parse_hours(venue["hours"])
        slots = []
        cursor = datetime.combine(datetime(2000, 1, 1), start)
        end_dt = datetime.combine(datetime(2000, 1, 1), end)
        from datetime import timedelta
        while cursor <= end_dt:
            hhmm = cursor.strftime("%H:%M")
            slots.append({"time": hhmm, "covers_booked": by_slot.get(hhmm, 0)})
            cursor += timedelta(minutes=SLOT_MINUTES)

        return {
            "venue_id": venue_id,
            "venue_name": venue["name"],
            "date": date,
            "capacity": venue["capacity"],
            "covers_booked": booked,
            "covers_remaining": remaining,
            "slots": slots,
        }
    finally:
        conn.close()


def _profile_full_name(conn: sqlite3.Connection, guest_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT first_name || ' ' || last_name AS name FROM guest_profiles "
        "WHERE guest_id = ?", (guest_id,)).fetchone()
    return row["name"] if row else None


def check_conflict(guest_id: str, reservation_date: str,
                   reservation_time: str) -> dict:
    """Find the guest's active reservation at the same date and time, if
    any (name-match rule applies)."""
    conn = get_conn()
    try:
        profile_name = _profile_full_name(conn, guest_id)
        row = conn.execute(
            "SELECT * FROM reservations WHERE guest_id = ? AND guest_name = ? "
            "AND reservation_date = ? AND reservation_time = ? "
            "AND status = 'Confirmed' LIMIT 1",
            (guest_id, profile_name or "", _validate_date(reservation_date),
             reservation_time)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"conflict": False, "existing_reservation": None,
                "message": "No conflicting reservation at that date and time."}
    existing = dict(row)
    return {
        "conflict": True,
        "existing_reservation": existing,
        "message": (f"The guest already has a Confirmed reservation at "
                    f"{existing['venue_name']} on "
                    f"{existing['reservation_date']} at "
                    f"{existing['reservation_time']} (party of "
                    f"{existing['party_size']}, confirmation "
                    f"{existing['confirmation_number']})."),
    }


def _check_ownership(reservation: dict, acting_guest_id: str) -> None:
    """Ownership requires BOTH guest_id and stored guest_name to match
    the guest's profile; mismatched rows read as another guest's."""
    conn = get_conn()
    try:
        profile_name = _profile_full_name(conn, acting_guest_id)
    finally:
        conn.close()
    if (reservation["guest_id"] != acting_guest_id
            or reservation["guest_name"] != profile_name):
        raise StoreError("forbidden", "This reservation belongs to a different guest.")


def list_reservations(guest_id: Optional[str] = None,
                      status: Optional[str] = None,
                      from_date: Optional[str] = None,
                      to_date: Optional[str] = None,
                      venue_id: Optional[str] = None) -> list[dict]:
    """List reservations with optional filters. When filtering by guest,
    a row counts only if BOTH guest_id and stored guest_name match the
    profile (mismatched seed rows are logged at startup and excluded)."""
    conn = get_conn()
    try:
        sql, params = "SELECT * FROM reservations WHERE 1=1", []
        if guest_id:
            profile_name = _profile_full_name(conn, guest_id)
            sql += " AND guest_id = ? AND guest_name = ?"
            params.extend([guest_id, profile_name or ""])
        if status:
            sql += " AND status = ?"
            params.append(status)
        if from_date:
            sql += " AND reservation_date >= ?"
            params.append(_validate_date(from_date))
        if to_date:
            sql += " AND reservation_date <= ?"
            params.append(_validate_date(to_date))
        if venue_id:
            venue = resolve_venue_ref(venue_id)
            if venue is None:
                raise StoreError(
                    "unknown_venue",
                    f"'{venue_id}' does not match any restaurant by ID or "
                    f"name. Valid: "
                    + ", ".join(f"{v['name']} ({vid})"
                                for vid, v in RESTAURANTS.items()))
            sql += " AND venue_id = ?"
            params.append(venue["venue_id"])
        sql += " ORDER BY reservation_date, reservation_time"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_reservation(reservation_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        if not row:
            raise StoreError("not_found", f"Reservation '{reservation_id}' not found.")
        return dict(row)
    finally:
        conn.close()


def create_reservation(*, guest_id: str, guest_name: str, venue_id: str,
                       reservation_date: str, reservation_time: str,
                       party_size: int, cabin_number: Optional[str] = None,
                       special_requests: Optional[str] = None,
                       dietary_notes: Optional[str] = None) -> dict:
    venue = _validate_venue(venue_id)
    _validate_date(reservation_date)
    _validate_time(reservation_time, venue)
    _validate_party_size(party_size)

    conn = get_conn()
    try:
        with conn:
            _check_capacity(conn, venue, reservation_date, party_size)
            rid = _new_reservation_id(conn)
            record = {
                "reservation_id": rid,
                "guest_id": guest_id,
                "guest_name": guest_name,
                "cabin_number": cabin_number,
                "venue_id": venue_id,
                "venue_name": venue["name"],
                "reservation_date": reservation_date,
                "reservation_time": reservation_time,
                "party_size": party_size,
                "special_requests": special_requests,
                "dietary_notes": dietary_notes,
                "status": "Confirmed",
                "confirmation_number": _new_confirmation_number(),
                "created_at": _now(),
                "modified_at": None,
                "cancelled_at": None,
                "cancellation_reason": None,
            }
            conn.execute(
                f"INSERT INTO reservations ({', '.join(record)}) "
                f"VALUES ({', '.join(':' + k for k in record)})",
                record,
            )
        return record
    finally:
        conn.close()


def modify_reservation(reservation_id: str, *, acting_guest_id: Optional[str] = None,
                       **changes) -> dict:
    """Update a reservation. Only whitelisted fields may change; ownership is
    enforced server-side when `acting_guest_id` is supplied."""
    allowed = {"reservation_date", "reservation_time", "party_size",
               "special_requests", "dietary_notes"}
    unknown = set(changes) - allowed
    if unknown:
        raise StoreError("invalid_field", f"Cannot modify field(s): {', '.join(sorted(unknown))}.")
    if not changes:
        raise StoreError("no_changes", "No changes were provided.")

    current = get_reservation(reservation_id)
    if acting_guest_id:
        _check_ownership(current, acting_guest_id)
    if current["status"] == "Cancelled":
        raise StoreError("already_cancelled",
                         "This reservation was cancelled. Please create a new one instead.")

    venue = RESTAURANTS[current["venue_id"]]
    new_date = changes.get("reservation_date", current["reservation_date"])
    new_time = changes.get("reservation_time", current["reservation_time"])
    new_size = int(changes.get("party_size", current["party_size"]))
    _validate_date(new_date)
    _validate_time(new_time, venue)
    _validate_party_size(new_size)

    conn = get_conn()
    try:
        with conn:
            _check_capacity(conn, venue, new_date, new_size, exclude_id=reservation_id)
            changes["modified_at"] = _now()
            sets = ", ".join(f"{k} = :{k}" for k in changes)
            conn.execute(
                f"UPDATE reservations SET {sets} WHERE reservation_id = :rid",
                changes | {"rid": reservation_id},
            )
        return get_reservation(reservation_id)
    finally:
        conn.close()


def cancel_reservation(reservation_id: str, *, acting_guest_id: Optional[str] = None,
                       reason: Optional[str] = None) -> dict:
    current = get_reservation(reservation_id)
    if acting_guest_id:
        _check_ownership(current, acting_guest_id)
    if current["status"] == "Cancelled":
        raise StoreError("already_cancelled", "This reservation is already cancelled.")

    conn = get_conn()
    try:
        with conn:
            conn.execute(
                "UPDATE reservations SET status = 'Cancelled', cancelled_at = ?, "
                "cancellation_reason = ? WHERE reservation_id = ?",
                (_now(), reason or "Guest requested", reservation_id),
            )
        return get_reservation(reservation_id)
    finally:
        conn.close()
