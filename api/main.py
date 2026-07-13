"""Dining Reservation REST API (FastAPI) — the system of record for
reservations; the MCP server is a thin client of this service."""

import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Allow `python api/main.py` and `uvicorn api.main:app` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api import store
from api.store import StoreError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dining_api")

app = FastAPI(
    title="Ocean Cruises Dining Reservation API",
    version="1.0.0",
    description="Specialty dining reservations for Ocean Cruises.",
)

# StoreError codes → HTTP statuses
STATUS_MAP = {
    "venue_not_found": 404,
    "not_found": 404,
    "forbidden": 403,
    "no_capacity": 409,
    "already_cancelled": 409,
    "invalid_date": 422,
    "invalid_time": 422,
    "outside_hours": 422,
    "invalid_party_size": 422,
    "invalid_field": 422,
    "no_changes": 422,
}


@app.on_event("startup")
def startup() -> None:
    from data.database import init_database
    seeded = init_database()
    fresh = {k: v for k, v in seeded.items() if v}
    if fresh:
        log.info("Seeded from CSV: %s.",
                 ", ".join(f"{v} {k}" for k, v in fresh.items()))
    else:
        log.info("Existing database found — skipping seed (warm restart).")


@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    log.info("%s %s -> %d (%.0f ms)", request.method, request.url.path,
             response.status_code, (time.perf_counter() - start) * 1000)
    return response


@app.exception_handler(StoreError)
async def store_error_handler(_: Request, exc: StoreError):
    return JSONResponse(
        status_code=STATUS_MAP.get(exc.code, 400),
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ── Request models ─────────────────────────────────────────────────────────

class CreateReservation(BaseModel):
    guest_id: str = Field(..., examples=["G100001"])
    guest_name: str = Field(..., examples=["James Smith"])
    venue_id: str = Field(..., examples=["DIN001"])
    reservation_date: str = Field(..., description="YYYY-MM-DD")
    reservation_time: str = Field(..., description="HH:MM 24-hour")
    party_size: int = Field(..., ge=1, le=8)
    cabin_number: Optional[str] = None
    special_requests: Optional[str] = None
    dietary_notes: Optional[str] = None


class ModifyReservation(BaseModel):
    reservation_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    reservation_time: Optional[str] = Field(None, description="HH:MM 24-hour")
    party_size: Optional[int] = Field(None, ge=1, le=8)
    special_requests: Optional[str] = None
    dietary_notes: Optional[str] = None


class CancelReservation(BaseModel):
    reason: Optional[str] = None


# ── Endpoints (the six required) ────────────────────────

@app.get("/api/v1/restaurants")
def list_restaurants():
    return {"restaurants": store.list_restaurants()}


@app.get("/api/v1/restaurants/{venue_id}/availability")
def availability(venue_id: str, date: str = Query(..., description="YYYY-MM-DD")):
    return store.get_availability(venue_id, date)


@app.get("/api/v1/reservations")
def list_reservations(
    guest_id: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    venue_id: Optional[str] = Query(None, description="Filter to one restaurant, e.g. DIN004"),
):
    reservations = store.list_reservations(
        guest_id=guest_id, status=status, from_date=from_date,
        to_date=to_date, venue_id=venue_id)
    return {"reservations": reservations, "count": len(reservations)}


@app.get("/api/v1/reservations/conflict")
def check_reservation_conflict(
    guest_id: str,
    reservation_date: str = Query(..., description="YYYY-MM-DD"),
    reservation_time: str = Query(..., description="HH:MM, 24-hour"),
):
    return store.check_conflict(guest_id, reservation_date, reservation_time)


@app.post("/api/v1/reservations", status_code=201)
def create_reservation(body: CreateReservation):
    return store.create_reservation(**body.model_dump())


@app.patch("/api/v1/reservations/{reservation_id}")
def modify_reservation(
    reservation_id: str,
    body: ModifyReservation,
    guest_id: Optional[str] = Query(
        None, description="Acting guest — ownership enforced when provided"),
):
    changes = body.model_dump(exclude_none=True)
    return store.modify_reservation(reservation_id, acting_guest_id=guest_id, **changes)


@app.delete("/api/v1/reservations/{reservation_id}")
def cancel_reservation(
    reservation_id: str,
    body: Optional[CancelReservation] = None,
    guest_id: Optional[str] = Query(
        None, description="Acting guest — ownership enforced when provided"),
):
    reason = body.reason if body else None
    return store.cancel_reservation(reservation_id, acting_guest_id=guest_id, reason=reason)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
