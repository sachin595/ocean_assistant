"""
Text2SQL golden integration set — execution accuracy.

Requires OPENAI_API_KEY and the seed CSVs. Run:
    pytest tests/test_text2sql_integration.py -m text2sql -v

Two different SQL statements can both be correct, so these tests never
compare SQL strings. Each question's ground truth is computed here in
Python directly from the seed CSVs, and the pipeline's *returned values*
are compared against it. Uses a fresh temporary database so runtime
reservation changes in a developer's working copy don't affect results.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from config import DATA_DIR, OPENAI_API_KEY
from data import database

pytestmark = [
    pytest.mark.text2sql,
    pytest.mark.skipif(not OPENAI_API_KEY, reason="OPENAI_API_KEY not set"),
]

GUEST = "G100036"  # a guest with substantial folio activity


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    test_db = tmp_path_factory.mktemp("golden") / "golden.db"
    original = database.DB_PATH
    database.DB_PATH = test_db
    database.init_database()
    yield test_db
    database.DB_PATH = original


@pytest.fixture(scope="module")
def service():
    from config import ASSISTANT_TODAY
    from text2sql.service import Text2SQLService
    return Text2SQLService(ASSISTANT_TODAY)


@pytest.fixture(scope="module")
def profile():
    with open(DATA_DIR / "guest_profiles.csv", newline="", encoding="utf-8") as f:
        return next(r for r in csv.DictReader(f) if r["Guest_ID"] == GUEST)


@pytest.fixture(scope="module")
def folio():
    with open(DATA_DIR / "folio_transactions.csv", newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["Guest_ID"] == GUEST]


@pytest.fixture(scope="module")
def reservations():
    with open(DATA_DIR / "dining_reservations.csv", newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["Guest_ID"] == GUEST]


def ask(service, question):
    result = service.answer_question(question, GUEST)
    assert "error" not in result, f"pipeline failed for: {question!r} -> {result}"
    return result


def flatten(result):
    return [value for row in result["rows"] for value in row]


def numbers(result):
    return [v for v in flatten(result) if isinstance(v, (int, float))]


def assert_contains_number(result, expected, tolerance=0.01):
    assert any(abs(float(v) - expected) <= tolerance for v in numbers(result)), (
        f"expected {expected} in {result['rows']}")


# ── Golden questions ───────────────────────────────────────────────────────

def test_cabin_number(db, service, profile):
    result = ask(service, "What is my cabin number?")
    assert profile["Cabin_Number"] in [str(v) for v in flatten(result)]


def test_loyalty_points(db, service, profile):
    result = ask(service, "How many loyalty points do I have?")
    assert_contains_number(result, float(profile["Loyalty_Points"]), 0)


def test_total_posted_charges(db, service, folio):
    expected = sum(float(r["Total"]) for r in folio if r["Status"] == "Posted")
    result = ask(service, "What are my total posted charges?")
    assert_contains_number(result, expected)


def test_spa_spend(db, service, folio):
    expected = sum(float(r["Total"]) for r in folio if r["Category"] == "Spa")
    result = ask(service, "How much have I spent at the spa in total?")
    assert_contains_number(result, expected)


def test_dining_charges_listed(db, service, folio):
    expected_count = sum(1 for r in folio if r["Category"] == "Dining")
    result = ask(service, "Show my dining charges.")
    assert result["row_count"] == expected_count


def test_disputed_charges(db, service, folio):
    expected_count = sum(1 for r in folio if r["Status"] == "Disputed")
    result = ask(service, "Do I have any disputed charges? List them.")
    assert result["row_count"] == expected_count


def test_spend_by_category(db, service, folio):
    by_category = {}
    for r in folio:
        by_category[r["Category"]] = by_category.get(r["Category"], 0.0) + float(r["Total"])
    result = ask(service, "How much did I spend by category?")
    assert result["row_count"] == len(by_category)
    for expected in by_category.values():
        assert_contains_number(result, expected)


def test_charges_in_date_range(db, service, folio):
    expected = sum(float(r["Total"]) for r in folio
                   if "2026-02-05" <= r["Transaction_Date"] <= "2026-02-07")
    result = ask(service, "What were my total charges between "
                          "February 5 and February 7, 2026?")
    assert_contains_number(result, expected)


def test_largest_transaction(db, service, folio):
    expected = max(float(r["Total"]) for r in folio)
    result = ask(service, "What is my largest transaction?")
    assert_contains_number(result, expected)


def test_service_charge_total(db, service, folio):
    expected = sum(float(r["Service_Charge"]) for r in folio)
    result = ask(service, "How much service charge have I paid in total?")
    assert_contains_number(result, expected)


def test_pending_transactions(db, service, folio):
    expected_count = sum(1 for r in folio if r["Status"] == "Pending")
    result = ask(service, "Show my pending transactions.")
    assert result["row_count"] == expected_count


def test_confirmed_reservation_count(db, service, reservations):
    expected = sum(1 for r in reservations if r["Status"] == "Confirmed")
    result = ask(service, "How many confirmed dining reservations do I have?")
    assert_contains_number(result, expected, 0)


def test_beverage_spend(db, service, folio):
    expected = sum(float(r["Total"]) for r in folio if r["Category"] == "Beverage")
    result = ask(service, "How much have I spent on drinks?")
    assert_contains_number(result, expected)


def test_transaction_count(db, service, folio):
    result = ask(service, "How many transactions are on my folio?")
    assert_contains_number(result, len(folio), 0)


def test_cross_guest_question_returns_own_data_only(db, service):
    result = ask(service, "List every distinct guest_id in my transactions.")
    ids = {str(v) for v in flatten(result)}
    assert ids <= {GUEST}
