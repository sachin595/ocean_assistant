"""
Approved schema for Text2SQL.

This is the only view of the database the SQL generator ever sees, and the
only surface the validator accepts. The physical `reservations` table is
exposed under the logical name `dining_reservations`; the mapping happens
in the guest-scoping layer.
"""

APPROVED_TABLES = {
    "guest_profiles",
    "folio_transactions",
    "dining_reservations",
}

# Logical table name -> physical table name (used when building scope CTEs).
PHYSICAL_TABLES = {
    "guest_profiles": "guest_profiles",
    "folio_transactions": "folio_transactions",
    "dining_reservations": "reservations",
}

TABLE_COLUMNS = {
    "guest_profiles": [
        "guest_id", "first_name", "last_name", "cabin_number",
        "cabin_category", "deck", "loyalty_tier", "loyalty_points",
        "party_size", "embark_date", "debark_date",
        "dietary_restrictions", "special_occasions",
        "beverage_package", "past_cruises",
    ],
    "folio_transactions": [
        "transaction_id", "guest_id", "cabin_number", "transaction_date",
        "transaction_time", "category", "description", "venue", "quantity",
        "unit_price", "amount", "service_charge", "total", "status",
        "reference_id", "posted_by", "notes",
    ],
    "dining_reservations": [
        "reservation_id", "guest_id", "guest_name", "cabin_number",
        "venue_id", "venue_name", "reservation_date", "reservation_time",
        "party_size", "special_requests", "dietary_notes", "status",
        "confirmation_number", "created_at", "modified_at",
        "cancelled_at", "cancellation_reason",
    ],
}


def schema_prompt(today: str) -> str:
    """Schema description given to the SQL generator model."""
    return f"""You write SQLite queries over the current guest's cruise data.

TODAY'S DATE (ship time): {today}

All three tables below contain ONLY the current guest's rows — they are
pre-filtered by the system. NEVER filter by guest_id, guest name, or cabin;
never reference any specific guest identifier value.

TABLE guest_profiles (exactly one row — the current guest)
  guest_id TEXT, first_name TEXT, last_name TEXT, cabin_number TEXT,
  cabin_category TEXT ('Interior','Oceanview','Balcony','Suite'),
  deck INTEGER, loyalty_tier TEXT ('Blue','Silver','Gold','Platinum',
  'Diamond','Diamond Plus'), loyalty_points INTEGER, party_size INTEGER,
  embark_date TEXT (YYYY-MM-DD), debark_date TEXT (YYYY-MM-DD),
  dietary_restrictions TEXT, special_occasions TEXT,
  beverage_package TEXT, past_cruises INTEGER

TABLE folio_transactions (the guest's onboard account charges)
  transaction_id TEXT, guest_id TEXT, cabin_number TEXT,
  transaction_date TEXT (YYYY-MM-DD), transaction_time TEXT (HH:MM:SS),
  category TEXT ('Dining','Beverage','Spa','Excursion','Shopping',
  'Entertainment','Gratuity','Communication'), description TEXT,
  venue TEXT, quantity INTEGER, unit_price REAL,
  amount REAL   -- charge BEFORE service charge,
  service_charge REAL,
  total REAL    -- FINAL amount including service charge; use this for
                -- "how much did I spend" questions,
  status TEXT ('Posted','Pending','Disputed','Refunded'),
  reference_id TEXT  -- links dining charges to dining_reservations.reservation_id,
  posted_by TEXT, notes TEXT

TABLE dining_reservations (the guest's specialty dining reservations)
  reservation_id TEXT, guest_id TEXT, guest_name TEXT, cabin_number TEXT,
  venue_id TEXT, venue_name TEXT, reservation_date TEXT (YYYY-MM-DD),
  reservation_time TEXT (HH:MM 24-hour), party_size INTEGER,
  special_requests TEXT, dietary_notes TEXT,
  status TEXT ('Confirmed','Cancelled'), confirmation_number TEXT,
  created_at TEXT, modified_at TEXT, cancelled_at TEXT,
  cancellation_reason TEXT

RELATIONSHIPS
  folio_transactions.guest_id -> guest_profiles.guest_id
  dining_reservations.guest_id -> guest_profiles.guest_id
  folio_transactions.reference_id -> dining_reservations.reservation_id

BUSINESS MEANINGS
  - "spent" / "charges" / "spending" -> SUM(total) on folio_transactions
  - amount = pre-service-charge value; total = final billed value
  - "upcoming reservation" = status = 'Confirmed' AND reservation_date >= '{today}'
  - Money is in USD; dates compare lexicographically as YYYY-MM-DD strings
"""
