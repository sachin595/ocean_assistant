"""
RAG retrieval quality — golden-set evaluation.

For each question, assert the expected knowledge-base document appears in
the top-k retrieved chunks. This catches chunking/embedding regressions —
if a doc stops surfacing for its own topic, retrieval is broken regardless
of how fluent the final answer sounds.

Requires: a built index (`python rag/ingest.py`) and OPENAI_API_KEY in .env
(each question embeds one query). Run: `pytest tests/ -m retrieval -v`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# (question, expected source document) — one probe per KB doc, mixing
# table-borne facts (prices, hours) and prose-borne facts (policies).
GOLDEN_SET = [
    ("What time does the pool close?", "Ship Information & Policies Knowledge Base"),
    ("What's the dress code for dinner?", "Dining & Restaurant Knowledge Base"),
    ("What shows are playing in the theater?", "Entertainment & Activities Knowledge Base"),
    ("How much is a 50 minute Swedish massage?", "Spa & Wellness Knowledge Base"),
    ("How do tender operations work at port?", "Port & Destination Knowledge Base"),
    ("What is the refund if I cancel a shore excursion 30 hours before?", "Shore Excursion Knowledge Base"),
    ("What benefits do Platinum loyalty members get?", "Ocean Rewards Loyalty Knowledge Base"),
    ("How do I dispute a charge on my onboard account?", "Onboard Account & Billing Knowledge Base"),
    ("How do I request extra towels for my cabin?", "Guest Services Knowledge Base"),
    ("How far in advance can I book specialty dining?", "Reservation Management Knowledge Base"),
    ("What guest profile data can the assistant access?", "Guest Data Access Knowledge Base"),
    ("When should a request be escalated to a human?", "Escalation & Feedback Knowledge Base"),
]


@pytest.fixture(scope="module")
def retriever():
    from rag.retriever import Retriever
    return Retriever()


@pytest.mark.retrieval
@pytest.mark.parametrize("question,expected_source", GOLDEN_SET)
def test_expected_source_in_top_k(retriever, question, expected_source):
    results = retriever.search(question, top_k=4)
    sources = [r["source"] for r in results]
    assert expected_source in sources, (
        f"Expected '{expected_source}' in top-4 for {question!r}, got {sources}")

# Off-topic probes: nothing in the knowledge base should confidently match
# these, so the threshold must reject every chunk and return nothing.
OFF_TOPIC = [
    "What is the population of Brazil?",
    "Who won yesterday's basketball game?",
    "How do I repair a car engine?",
]


@pytest.mark.retrieval
@pytest.mark.parametrize("question,expected_source", GOLDEN_SET)
def test_golden_question_survives_the_relevance_threshold(
        retriever, question, expected_source):
    # search() already applies RAG_MIN_RELEVANCE — if the threshold is set
    # too high, on-topic questions start coming back empty and this fails.
    results = retriever.search(question, top_k=4)
    assert results, f"threshold rejected everything for {question!r}"
    assert expected_source in [r["source"] for r in results]


@pytest.mark.retrieval
@pytest.mark.parametrize("question", OFF_TOPIC)
def test_off_topic_question_is_rejected(retriever, question):
    # If the threshold is set too low, junk matches leak through here.
    results = retriever.search(question, top_k=4)
    assert results == [], (
        f"expected no confident match for {question!r}, got "
        f"{[(r['source'], r['relevance']) for r in results]}")
