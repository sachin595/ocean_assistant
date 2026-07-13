"""
Offline tests — no API key or running services required.

Covers the deterministic core: PDF chunking behavior and the reservation
store's business rules (validation, capacity, ownership, soft cancel,
warm-restart seeding). Run: `pytest tests/test_offline.py -v`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from api import store
from api.store import StoreError
from data import database
from rag.ingest import extract_chunks


# ── Chunking ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def chunks():
    return extract_chunks()


def test_all_twelve_documents_ingested(chunks):
    assert len({c.filename for c in chunks}) == 12


def test_tables_are_markdown_and_intact(chunks):
    tables = [c for c in chunks if c.chunk_type == "table"]
    assert len(tables) >= 30, "KB is table-heavy; expected many table chunks"
    for c in tables:
        assert "| --- |" in c.text or "| --- " in c.text, "table chunk must be markdown"


def test_spa_price_lives_in_a_table_chunk(chunks):
    """A known table-borne fact must survive extraction intact on one row."""
    spa_tables = [c.text for c in chunks
                  if c.chunk_type == "table" and "Spa" in c.source]
    assert any("Swedish Massage | 50 min | $159" in t for t in spa_tables)


def test_every_chunk_carries_citation_metadata(chunks):
    for c in chunks:
        assert c.source and c.page >= 1 and c.chunk_type in {"table", "text"}


# ── Reservation store (isolated temp DB) ──────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", test_db)
    seeded = database.init_database()
    assert seeded == {"reservations": 212, "guest_profiles": 200,
                      "folio_transactions": 4775}
    return test_db


def _create(**overrides):
    base = dict(guest_id="G100002", guest_name="Linda Smith", venue_id="DIN004",
                reservation_date="2026-02-10", reservation_time="19:00",
                party_size=2)
    return store.create_reservation(**(base | overrides))


def test_create_and_soft_cancel(db):
    created = _create()
    assert created["status"] == "Confirmed"
    assert created["confirmation_number"].startswith("DIN-")

    cancelled = store.cancel_reservation(created["reservation_id"],
                                         acting_guest_id="G100002")
    assert cancelled["status"] == "Cancelled"
    assert cancelled["cancelled_at"] is not None
    # Soft cancel: the row still exists.
    assert store.get_reservation(created["reservation_id"])


def test_warm_restart_does_not_reseed(db):
    created = _create()
    reseeded = database.init_database()
    assert all(count == 0 for count in reseeded.values())
    assert store.get_reservation(created["reservation_id"])


def test_ownership_enforced(db):
    created = _create()
    with pytest.raises(StoreError) as err:
        store.modify_reservation(created["reservation_id"],
                                 acting_guest_id="G100099",
                                 reservation_time="20:00")
    assert err.value.code == "forbidden"


def test_party_size_and_hours_validation(db):
    with pytest.raises(StoreError) as err:
        _create(party_size=9)
    assert err.value.code == "invalid_party_size"
    with pytest.raises(StoreError) as err:
        _create(venue_id="DIN005", reservation_time="17:30")  # opens 18:00
    assert err.value.code == "outside_hours"


def test_capacity_enforced(db):
    # Chef's Table seats 24 on an empty date: 3 x 8 fills it, a 4th fails.
    for _ in range(3):
        _create(venue_id="DIN005", reservation_date="2026-02-20",
                reservation_time="18:30", party_size=8)
    with pytest.raises(StoreError) as err:
        _create(venue_id="DIN005", reservation_date="2026-02-20",
                reservation_time="19:00", party_size=2)
    assert err.value.code == "no_capacity"


def test_modify_does_not_count_against_itself(db):
    created = _create(venue_id="DIN005", reservation_date="2026-02-21",
                      reservation_time="18:30", party_size=8)
    for _ in range(2):
        _create(venue_id="DIN005", reservation_date="2026-02-21",
                reservation_time="19:00", party_size=8)
    # Venue is now full (24/24); changing this reservation's time must still
    # succeed because its own 8 covers are excluded from the check.
    updated = store.modify_reservation(created["reservation_id"],
                                       acting_guest_id="G100002",
                                       reservation_time="20:00")
    assert updated["reservation_time"] == "20:00"
