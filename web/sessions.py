"""
In-memory web session store.

Maps a secure random token (delivered as an HttpOnly cookie) to an
AssistantSession. Suitable for a single-process prototype.
"""

import secrets
import time
from typing import Optional

from assistant.session import AssistantSession

IDLE_TIMEOUT_SECONDS = 60 * 60  # sessions expire after an hour of inactivity


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, AssistantSession] = {}

    def create(self, session: AssistantSession) -> str:
        self._prune()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = session
        return token

    def get(self, token: Optional[str]) -> Optional[AssistantSession]:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if time.time() - session.last_activity > IDLE_TIMEOUT_SECONDS:
            self._sessions.pop(token, None)
            return None
        return session

    def remove(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        expired = [t for t, s in self._sessions.items()
                   if now - s.last_activity > IDLE_TIMEOUT_SECONDS]
        for token in expired:
            self._sessions.pop(token, None)
