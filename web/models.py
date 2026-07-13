"""Request and response models for the web API."""

from typing import Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    guest_id: str = Field(..., examples=["G100005"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class TurnResponse(BaseModel):
    text: str
    citations: list[dict] = []
    data_table: Optional[dict] = None
    reservation_card: Optional[dict] = None
    pending_action: Optional[dict] = None


class LoginResponse(BaseModel):
    guest_name: str
    guest_id: str
    loyalty_tier: Optional[str] = None
    turn: TurnResponse


class ErrorResponse(BaseModel):
    error: str
