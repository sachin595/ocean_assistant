"""Shared SQLite layer: schema, CSV seeding with venue-ID normalization,
and a startup data-quality report for guest-name mismatches."""

import csv
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from config import DATA_DIR, DB_PATH


def get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Read-write connection. DB_PATH is resolved at call time so tests and
    environment overrides that patch the module attribute take effect."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_readonly_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Read-only connection (URI mode=ro). Writes fail at the SQLite level
    regardless of what SQL is executed on it."""
    path = Path(db_path or DB_PATH)
    uri = f"file:///{path.as_posix().lstrip('/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_database(db_path: Optional[Path] = None,
                  data_dir: Optional[Path] = None) -> dict[str, int]:
    """Create all tables and indexes; seed each table only if empty.

    Returns {table_name: rows_seeded} — zeros on a warm restart.
    """
    data_dir = data_dir or DATA_DIR
    conn = get_conn(db_path)
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                reservation_id      TEXT PRIMARY KEY,
                guest_id            TEXT NOT NULL,
                guest_name          TEXT NOT NULL,
                cabin_number        TEXT,
                venue_id            TEXT NOT NULL,
                venue_name          TEXT NOT NULL,
                reservation_date    TEXT NOT NULL,
                reservation_time    TEXT NOT NULL,
                party_size          INTEGER NOT NULL,
                special_requests    TEXT,
                dietary_notes       TEXT,
                status              TEXT NOT NULL DEFAULT 'Confirmed',
                confirmation_number TEXT,
                created_at          TEXT,
                modified_at         TEXT,
                cancelled_at        TEXT,
                cancellation_reason TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_profiles (
                guest_id             TEXT PRIMARY KEY,
                first_name           TEXT NOT NULL,
                last_name            TEXT NOT NULL,
                cabin_number         TEXT,
                cabin_category       TEXT,
                deck                 INTEGER,
                loyalty_tier         TEXT,
                loyalty_points       INTEGER,
                party_size           INTEGER,
                embark_date          TEXT,
                debark_date          TEXT,
                dietary_restrictions TEXT,
                special_occasions    TEXT,
                beverage_package     TEXT,
                past_cruises         INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS folio_transactions (
                transaction_id   TEXT PRIMARY KEY,
                guest_id         TEXT NOT NULL,
                cabin_number     TEXT,
                transaction_date TEXT NOT NULL,
                transaction_time TEXT,
                category         TEXT,
                description      TEXT,
                venue            TEXT,
                quantity         INTEGER,
                unit_price       REAL,
                amount           REAL,
                service_charge   REAL,
                total            REAL,
                status           TEXT,
                reference_id     TEXT,
                posted_by        TEXT,
                notes            TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_guest "
                     "ON reservations (guest_id, reservation_date, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_venue_date "
                     "ON reservations (venue_id, reservation_date, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_guest "
                     "ON guest_profiles (guest_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_folio_guest "
                     "ON folio_transactions (guest_id, transaction_date, category, status)")

    seeded = {
        "reservations": _seed_reservations(conn, data_dir / "dining_reservations.csv"),
        "guest_profiles": _seed_profiles(conn, data_dir / "guest_profiles.csv"),
        "folio_transactions": _seed_folio(conn, data_dir / "folio_transactions.csv"),
    }
    _report_name_mismatches(conn)
    conn.close()
    return seeded


def _report_name_mismatches(conn: sqlite3.Connection) -> None:
    """Log every reservation whose stored guest_name disagrees with its
    guest_id's profile name; these rows are excluded from guest results."""
    rows = conn.execute(
        """
        SELECT r.reservation_id, r.guest_id, r.guest_name,
               p.first_name || ' ' || p.last_name AS profile_name
        FROM reservations r
        JOIN guest_profiles p ON p.guest_id = r.guest_id
        WHERE r.guest_name != p.first_name || ' ' || p.last_name
        """
    ).fetchall()
    if not rows:
        return
    log = logging.getLogger("data.quality")
    log.warning("Found %d reservation(s) whose stored guest_name does not "
                "match the guest's profile — these rows are excluded from "
                "guest-facing results by the name-match rule:", len(rows))
    for r in rows:
        log.warning("  %s: guest_id=%s stored_name=%r profile_name=%r",
                    r["reservation_id"], r["guest_id"],
                    r["guest_name"], r["profile_name"])


def _is_empty(conn: sqlite3.Connection, table: str) -> bool:
    (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return count == 0


# Official name -> ID mapping. The seed CSV's IDs are inconsistent with
# it, so venue_name is authoritative and IDs are normalized at import.
CANONICAL_VENUE_IDS = {
    "The Steakhouse": "DIN001",
    "La Trattoria": "DIN002",
    "Le Bistro": "DIN003",
    "Sakura": "DIN004",
    "Chef's Table": "DIN005",
}


def _seed_reservations(conn: sqlite3.Connection, csv_path: Path) -> int:
    if not _is_empty(conn, "reservations"):
        return 0
    rows = 0
    corrected = 0
    with conn, open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            canonical = CANONICAL_VENUE_IDS.get(row["Venue_Name"])
            if canonical and row["Venue_ID"] != canonical:
                row = dict(row) | {"Venue_ID": canonical}
                corrected += 1
            conn.execute(
                """
                INSERT INTO reservations VALUES
                (:Reservation_ID, :Guest_ID, :Guest_Name, :Cabin_Number,
                 :Venue_ID, :Venue_Name, :Reservation_Date, :Reservation_Time,
                 :Party_Size, :Special_Requests, :Dietary_Notes, :Status,
                 :Confirmation_Number, :Created_At, :Modified_At,
                 :Cancelled_At, :Cancellation_Reason)
                """,
                {k: (v or None) for k, v in row.items()}
                | {"Party_Size": int(row["Party_Size"])},
            )
            rows += 1
    if corrected:
        logging.getLogger("data.quality").warning(
            "Normalized venue_id on %d of %d reservation rows to match the "
            "official catalog (venue_name is authoritative; the seed CSV's "
            "IDs are inconsistent with it).", corrected, rows)
    return rows


def _seed_profiles(conn: sqlite3.Connection, csv_path: Path) -> int:
    if not _is_empty(conn, "guest_profiles"):
        return 0
    rows = 0
    with conn, open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            conn.execute(
                """
                INSERT INTO guest_profiles VALUES
                (:Guest_ID, :First_Name, :Last_Name, :Cabin_Number,
                 :Cabin_Category, :Deck, :Loyalty_Tier, :Loyalty_Points,
                 :Party_Size, :Embark_Date, :Debark_Date,
                 :Dietary_Restrictions, :Special_Occasions,
                 :Beverage_Package, :Past_Cruises)
                """,
                {k: (v or None) for k, v in row.items()}
                | {
                    "Deck": int(row["Deck"]) if row.get("Deck") else None,
                    "Loyalty_Points": int(row["Loyalty_Points"]) if row.get("Loyalty_Points") else 0,
                    "Party_Size": int(row["Party_Size"]) if row.get("Party_Size") else None,
                    "Past_Cruises": int(row["Past_Cruises"]) if row.get("Past_Cruises") else 0,
                },
            )
            rows += 1
    return rows


def _seed_folio(conn: sqlite3.Connection, csv_path: Path) -> int:
    if not _is_empty(conn, "folio_transactions"):
        return 0

    def num(value: str, cast):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    rows = 0
    with conn, open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            conn.execute(
                """
                INSERT INTO folio_transactions VALUES
                (:Transaction_ID, :Guest_ID, :Cabin_Number, :Transaction_Date,
                 :Transaction_Time, :Category, :Description, :Venue,
                 :Quantity, :Unit_Price, :Amount, :Service_Charge, :Total,
                 :Status, :Reference_ID, :Posted_By, :Notes)
                """,
                {k: (v or None) for k, v in row.items()}
                | {
                    "Quantity": num(row.get("Quantity"), int),
                    "Unit_Price": num(row.get("Unit_Price"), float),
                    "Amount": num(row.get("Amount"), float),
                    "Service_Charge": num(row.get("Service_Charge"), float),
                    "Total": num(row.get("Total"), float),
                },
            )
            rows += 1
    return rows
