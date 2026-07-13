"""Web interface: FastAPI app serving the chat UI and session-scoped
assistant endpoints."""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from assistant.runtime import AssistantRuntime, UnknownGuestError
from assistant.session import turn_to_dict
from config import PROJECT_ROOT
from web.models import ChatRequest, LoginRequest, LoginResponse, TurnResponse
from web.sessions import SessionStore

from assistant.telemetry import configure_logging, failure_stats

configure_logging(PROJECT_ROOT / "assistant.log")
log = logging.getLogger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "ocean_session"

runtime = AssistantRuntime()
sessions = SessionStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="Ocean Cruises Assistant Web", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

FRIENDLY_FAILURE = ("I'm terribly sorry — something went wrong on my end. "
                    "Please try that again, or visit Guest Services on "
                    "Deck 5 for immediate help.")


def _require_session(token: str | None):
    session = sessions.get(token)
    if session is None:
        raise HTTPException(status_code=401,
                            detail="Please log in with your Guest ID.")
    return session


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    """Liveness plus process-lifetime tool stats; guardrail rejections
    are reported separately from genuine failures."""
    return {"status": "ok", **failure_stats()}


@app.post("/api/login", response_model=LoginResponse)
async def login(body: LoginRequest, response: Response):
    try:
        session = await runtime.create_session(body.guest_id)
    except UnknownGuestError:
        raise HTTPException(
            status_code=404,
            detail="We couldn't find that Guest ID — please check your "
                   "cruise card and try again.")

    token = sessions.create(session)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")

    try:
        turn = await session.greet()
        turn_payload = turn_to_dict(turn)
    except Exception:
        log.exception("greeting_failed")
        turn_payload = {"text": f"Welcome aboard, {session.guest_name}! "
                                "How may I help you today?",
                        "citations": [], "data_table": None,
                        "reservation_card": None, "pending_action": None}

    return LoginResponse(
        guest_name=session.guest_name,
        guest_id=session.guest["Guest_ID"],
        loyalty_tier=session.guest.get("Loyalty_Tier"),
        turn=TurnResponse(**turn_payload),
    )


@app.post("/api/chat", response_model=TurnResponse)
async def chat(body: ChatRequest,
               ocean_session: str | None = Cookie(default=None)):
    session = _require_session(ocean_session)
    try:
        turn = await session.chat(body.message)
        return TurnResponse(**turn_to_dict(turn))
    except Exception:
        log.exception("chat_failed")
        return TurnResponse(text=FRIENDLY_FAILURE)


@app.post("/api/actions/{action_id}/confirm", response_model=TurnResponse)
async def confirm_action(action_id: str,
                         ocean_session: str | None = Cookie(default=None)):
    session = _require_session(ocean_session)
    try:
        turn = await session.confirm_pending(action_id)
        return TurnResponse(**turn_to_dict(turn))
    except Exception:
        log.exception("confirm_failed")
        return TurnResponse(text=FRIENDLY_FAILURE)


@app.post("/api/actions/{action_id}/cancel", response_model=TurnResponse)
async def cancel_action(action_id: str,
                        ocean_session: str | None = Cookie(default=None)):
    session = _require_session(ocean_session)
    try:
        turn = await session.cancel_pending(action_id)
        return TurnResponse(**turn_to_dict(turn))
    except Exception:
        log.exception("cancel_failed")
        return TurnResponse(text=FRIENDLY_FAILURE)


@app.post("/api/logout")
async def logout(response: Response,
                 ocean_session: str | None = Cookie(default=None)):
    sessions.remove(ocean_session)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
