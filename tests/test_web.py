"""
Web interface tests.

Requires OPENAI_API_KEY and a built knowledge index (`python rag/ingest.py`)
because application startup initializes the full runtime. The dining REST
API does not need to be running — these turns use no dining tools. Run:
    pytest tests/test_web.py -m web -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from config import OPENAI_API_KEY

pytestmark = [
    pytest.mark.web,
    pytest.mark.skipif(not OPENAI_API_KEY, reason="OPENAI_API_KEY not set"),
]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from web.app import app
    with TestClient(app) as test_client:
        yield test_client


def test_unknown_guest_login_rejected(client):
    response = client.post("/api/login", json={"guest_id": "G999999"})
    assert response.status_code == 404
    assert "Guest ID" in response.json()["detail"]


def test_chat_requires_session(client):
    client.cookies.clear()
    response = client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 401


def test_login_returns_greeting_and_cookie(client):
    response = client.post("/api/login", json={"guest_id": "G100005"})
    assert response.status_code == 200
    body = response.json()
    assert body["guest_name"] == "Michael Williams"
    assert body["turn"]["text"]
    assert "ocean_session" in response.cookies


def test_simple_chat_turn(client):
    client.post("/api/login", json={"guest_id": "G100005"})
    response = client.post("/api/chat", json={"message": "Hi there!"})
    assert response.status_code == 200
    body = response.json()
    assert body["text"]
    assert body["pending_action"] is None


def test_logout_invalidates_session(client):
    client.post("/api/login", json={"guest_id": "G100005"})
    client.post("/api/logout")
    response = client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 401
