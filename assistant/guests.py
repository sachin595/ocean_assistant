"""Guest identification — lookups against data/guest_profiles.csv.

The profile is the source of truth for the guest's identity (a handful of
seed reservation rows carry inconsistent Guest_Name values; Guest_ID is
treated as authoritative throughout the system).
"""

import csv
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DATA_DIR

GUEST_ID_PATTERN = re.compile(r"^G\d{6}$")


def load_profiles() -> dict[str, dict]:
    path = DATA_DIR / "guest_profiles.csv"
    with open(path, newline="", encoding="utf-8") as f:
        return {row["Guest_ID"]: row for row in csv.DictReader(f)}


_PROFILES = load_profiles()


def get_guest(guest_id: str) -> Optional[dict]:
    """Exact, case-sensitive Guest_ID lookup (uppercase G + six digits);
    'g100005' is rejected rather than normalized."""
    candidate = (guest_id or "").strip()
    if not GUEST_ID_PATTERN.match(candidate):
        return None
    return _PROFILES.get(candidate)


def profile_summary(profile: dict) -> str:
    """Compact profile block injected into the system prompt for
    personalization (loyalty tier, dietary needs, special occasions)."""
    name = f"{profile['First_Name']} {profile['Last_Name']}"
    lines = [
        f"Name: {name}",
        f"Guest ID: {profile['Guest_ID']}",
        f"Cabin: {profile['Cabin_Number']} ({profile['Cabin_Category']}, Deck {profile['Deck']})",
        f"Loyalty: {profile['Loyalty_Tier']} tier, {profile['Loyalty_Points']} points",
        f"Party size on booking: {profile['Party_Size']}",
        f"Sailing: {profile['Embark_Date']} to {profile['Debark_Date']}",
        f"Dietary restrictions: {profile['Dietary_Restrictions']}",
        f"Special occasions: {profile['Special_Occasions']}",
        f"Beverage package: {profile['Beverage_Package']}",
        f"Past cruises: {profile['Past_Cruises']}",
    ]
    return "\n".join(lines)
