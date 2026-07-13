"""Central configuration loaded from environment variables and the local .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_SQL_MODEL = os.getenv("OPENAI_SQL_MODEL", os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"))
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

DINING_API_BASE_URL = os.getenv("DINING_API_BASE_URL", "http://127.0.0.1:8000")
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", PROJECT_ROOT / "chroma_db"))
# Minimum cosine similarity a retrieved chunk needs before we trust it.
# Picked by running the golden retrieval questions (which should pass) and
# a handful of off-topic probes (which should be rejected) — see
# tests/test_retrieval.py. Tune via env.
RAG_MIN_RELEVANCE = float(os.getenv("RAG_MIN_RELEVANCE", "0.30"))

CHROMA_COLLECTION = "ocean_knowledge_base"

DATA_DIR = PROJECT_ROOT / "data"
KB_DIR = PROJECT_ROOT / "knowledge_base"
DB_PATH = PROJECT_ROOT / "ocean_data.db"

# The seed data lives entirely in the Feb 5-11, 2026 sailing window, so the
# assistant's notion of "today" is pinned inside it. Relative dates
# ("tomorrow at 7 PM") resolve against the data instead of wall-clock time.
ASSISTANT_TODAY = os.getenv("ASSISTANT_TODAY", "2026-02-07")


def require_openai_key() -> str:
    """Fail fast with a clear message if the API key is missing."""
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return OPENAI_API_KEY
