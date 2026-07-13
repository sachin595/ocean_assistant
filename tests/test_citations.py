"""Citation and relevance-threshold behavior, offline.

The retriever filters chunks by relevance before the model sees them, and
a small dedicated selection step decides which of the survivors actually
earn a citation — two documents can restate the same fact, and a
single-fact question should cite the one that answers it, not both.
These tests pin that down: single-source shortcuts, mocked multi-source
decisions, the all-rejected fallback, and the filename -> title mapping.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from assistant.guests import get_guest
from assistant.tools import ToolExecutor
from rag.citation_selector import select_sources
from rag.retriever import filter_by_relevance
from rag.source_names import SOURCE_NAMES, official_title

SPA = "Spa & Wellness Knowledge Base"
SHIP = "Knowledge Agent Knowledge Base (Ship Policies)"


class FakeRetriever:
    """Stands in for the real retriever: returns whatever chunks a test
    hands it, exactly as the real one would after threshold filtering."""

    def __init__(self, chunks):
        self._chunks = chunks

    def search(self, query, top_k=4):
        return self._chunks


def make_executor(chunks):
    guest = get_guest("G100036")
    return ToolExecutor(mcp_session=None, retriever=FakeRetriever(chunks),
                        text2sql=None, guest=guest)


def chunk(source, filename, text="Some passage.", relevance=0.5):
    return {"text": text, "source": source, "source_document": filename,
            "page": 1, "relevance": relevance}


def fake_openai_returning(indices):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps(indices)))])
    return client


# ── Single source: no decision to make, no model call needed ──────────────

@pytest.mark.asyncio
async def test_four_chunks_same_pdf_produce_one_citation_no_model_call():
    chunks = [chunk(SPA, "04_spa_wellness_kb.pdf")] * 4
    ex = make_executor(chunks)
    ex.reset_turn()
    with patch("rag.citation_selector.OpenAI") as mock_openai:
        await ex.execute("search_knowledge_base", {"query": "spa hours"})
        mock_openai.assert_not_called()
    assert ex.turn.citations == [{"source": SPA}]


def test_selector_skips_the_model_when_only_one_document_is_present():
    chunks = [chunk(SPA, "04_spa_wellness_kb.pdf")] * 3
    assert select_sources("spa hours", chunks, client=MagicMock()) == \
        ["04_spa_wellness_kb.pdf"]


# ── Genuine cross-document decision ────────────────────────────────────────

def test_selector_picks_the_directly_relevant_document_when_content_overlaps():
    # Two documents both mention fitness-center hours; the model should be
    # asked, and its pick is what gets cited -- not both automatically.
    chunks = [chunk(SPA, "04_spa_wellness_kb.pdf",
                    "6. Fitness Center: Deck 13, Hours 6AM-10PM, Complimentary"),
             chunk("Entertainment Agent Knowledge Base", "03_entertainment_agent_kb.pdf",
                   "Fitness Center Deck 13 6AM-10PM Free")]
    client = fake_openai_returning([1])
    assert select_sources("what time does the fitness center open?",
                          chunks, client=client) == ["04_spa_wellness_kb.pdf"]
    client.chat.completions.create.assert_called_once()


def test_selector_can_pick_multiple_documents_for_a_genuinely_mixed_question():
    chunks = [chunk("Loyalty Agent Knowledge Base", "07_loyalty_agent_kb.pdf",
                    "Platinum members get 30% off spa services."),
             chunk(SPA, "04_spa_wellness_kb.pdf",
                   "Thermal suite day pass: $45.")]
    client = fake_openai_returning([1, 2])
    picked = select_sources("Platinum spa discount, and the thermal pass price?",
                            chunks, client=client)
    assert picked == ["07_loyalty_agent_kb.pdf", "04_spa_wellness_kb.pdf"]


def test_selector_falls_back_to_top_passage_on_a_bad_model_response():
    chunks = [chunk(SPA, "04_spa_wellness_kb.pdf"),
             chunk(SHIP, "01_knowledge_agent_kb.pdf")]
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="not json"))])
    assert select_sources("q", chunks, client=client) == ["04_spa_wellness_kb.pdf"]


def test_selector_ignores_indices_the_model_was_never_given():
    chunks = [chunk(SPA, "04_spa_wellness_kb.pdf"),
             chunk(SHIP, "01_knowledge_agent_kb.pdf")]
    client = fake_openai_returning([1, 99])  # 99 doesn't exist
    assert select_sources("q", chunks, client=client) == ["04_spa_wellness_kb.pdf"]


# ── Citations never carry internal fields ──────────────────────────────────

@pytest.mark.asyncio
async def test_citations_never_carry_pages_filenames_or_scores():
    chunks = [chunk(SPA, "04_spa_wellness_kb.pdf")]
    ex = make_executor(chunks)
    ex.reset_turn()
    result = json.loads(await ex.execute("search_knowledge_base",
                                         {"query": "spa"}))
    assert set(ex.turn.citations[0]) == {"source"}
    for passage in result["results"]:
        assert set(passage) == {"text", "source"}


# ── All chunks rejected ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_rejected_gives_fallback_note_and_no_citations():
    ex = make_executor([])
    ex.reset_turn()
    result = json.loads(await ex.execute("search_knowledge_base",
                                         {"query": "population of Brazil"}))
    assert result["results"] == []
    assert "not certain" in result["note"] or "confident" in result["note"]
    assert "general knowledge" in result["note"]
    assert ex.turn.citations == []


# ── Threshold filter ───────────────────────────────────────────────────────

def test_filter_keeps_only_chunks_at_or_above_threshold():
    chunks = [chunk(SPA, "x.pdf", relevance=0.62), chunk(SPA, "x.pdf", relevance=0.30),
              chunk(SHIP, "y.pdf", relevance=0.29), chunk(SHIP, "y.pdf", relevance=0.05)]
    kept = filter_by_relevance(chunks, threshold=0.30)
    assert [c["relevance"] for c in kept] == [0.62, 0.30]


def test_filter_rejects_everything_when_all_scores_are_weak():
    chunks = [chunk(SPA, "x.pdf", relevance=0.12), chunk(SHIP, "y.pdf", relevance=0.08)]
    assert filter_by_relevance(chunks, threshold=0.30) == []


# ── Source-name mapping ────────────────────────────────────────────────────

def test_all_twelve_pdfs_have_official_titles():
    assert len(SOURCE_NAMES) == 12
    for filename, title in SOURCE_NAMES.items():
        assert filename.endswith(".pdf")
        assert "Knowledge Base" in title
        assert official_title(filename) == title


def test_official_titles_match_the_expected_naming_convention():
    # "<topic>_agent_kb.pdf" -> "<Topic> Agent Knowledge Base"; the two
    # filenames without "_agent_" keep their own descriptive name.
    assert official_title("07_loyalty_agent_kb.pdf") == "Loyalty Agent Knowledge Base"
    assert official_title("06_excursion_agent_kb.pdf") == "Excursion Agent Knowledge Base"
    assert official_title("01_knowledge_agent_kb.pdf") == \
        "Knowledge Agent Knowledge Base (Ship Policies)"
    assert official_title("04_spa_wellness_kb.pdf") == SPA


def test_unknown_filename_never_leaks_a_raw_pdf_name():
    title = official_title("99_new_policy_doc.pdf")
    assert ".pdf" not in title
    assert title == "99 New Policy Doc"
