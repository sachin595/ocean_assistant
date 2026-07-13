"""Deciding which retrieved passages actually earn a citation.

Relevance filtering answers "is this chunk good enough to use at all."
It doesn't answer "does the final answer to THIS question actually need
this document." Two PDFs can restate the exact same fact — citing both
for a single-fact question would be technically true but misleading about
which document is the real source. This is a judgment call about meaning,
not a number, so it gets one small, cheap, tightly-scoped model call
rather than a hand-coded scoring rule.
"""

import json
import logging

from openai import OpenAI

from config import OPENAI_CHAT_MODEL

log = logging.getLogger("rag.citation_selector")

SELECTION_PROMPT = """\
Choosing citations for a cruise-ship assistant's answer.

Question: {question}

Passages:
{passages}

Pick only the passage numbers genuinely needed to answer this question —
the minimum authoritative set. If two passages restate the same fact from
different documents, pick the one that answers it more directly, not
both. Pick more than one only when the question truly needs information
from more than one topic.

Return ONLY a JSON array of passage numbers, e.g. [1] or [1, 3]."""


def select_sources(question: str, passages: list[dict],
                   client: OpenAI = None) -> list[str]:
    """passages: accepted retrieval results (each with text and
    source_document). Returns the filenames that should be cited, deduped
    and in the order they were picked — never a filename outside what was
    actually retrieved."""
    distinct = list(dict.fromkeys(p["source_document"] for p in passages))
    if len(distinct) <= 1:
        return distinct  # nothing to decide between

    numbered = "\n".join(
        f"[{i + 1}] ({p['source_document']}): {p['text'][:400]}"
        for i, p in enumerate(passages))
    client = client or OpenAI()
    try:
        resp = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[{"role": "user", "content": SELECTION_PROMPT.format(
                question=question, passages=numbered)}],
            temperature=0,
            max_tokens=50,
        )
        picked = json.loads(resp.choices[0].message.content or "[]")
    except Exception:
        log.warning("citation_selection_failed question=%r falling back to "
                    "top passage", question[:60])
        picked = [1]

    valid = [i for i in picked if isinstance(i, int) and 1 <= i <= len(passages)]
    if not valid:
        valid = [1]

    filenames = []
    for i in valid:
        fn = passages[i - 1]["source_document"]
        if fn not in filenames:
            filenames.append(fn)
    return filenames
