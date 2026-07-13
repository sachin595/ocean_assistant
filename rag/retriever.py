"""Query-time search over the ChromaDB knowledge index.

The query is embedded with the same model used at ingest time, matched by
cosine similarity, then filtered: chunks below RAG_MIN_RELEVANCE are
dropped before they ever reach the model. Weak matches answering an
off-topic question are worse than no answer, so an empty result here is a
feature — the assistant falls back to asking the guest to rephrase.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb
from openai import OpenAI

from config import (CHROMA_COLLECTION, CHROMA_DIR, OPENAI_EMBEDDING_MODEL,
                    RAG_MIN_RELEVANCE, require_openai_key)
from rag.source_names import official_title

log = logging.getLogger("rag.retriever")

TOP_K = 4


def filter_by_relevance(chunks: list[dict],
                        threshold: float = RAG_MIN_RELEVANCE) -> list[dict]:
    """Keep only chunks at or above the relevance threshold. Relevance is
    cosine similarity (1 - distance), so higher is always better."""
    return [c for c in chunks if c["relevance"] >= threshold]


class Retriever:
    def __init__(self) -> None:
        require_openai_key()
        self._openai = OpenAI()
        chroma = chromadb.PersistentClient(path=str(CHROMA_DIR))
        try:
            self._collection = chroma.get_collection(CHROMA_COLLECTION)
        except Exception as exc:
            raise RuntimeError(
                "Knowledge index not found. Run `python rag/ingest.py` first."
            ) from exc

    def search(self, query: str, top_k: int = TOP_K) -> list[dict]:
        """Top-k chunks that pass the relevance threshold. Each carries
        text, the official source title, and internal fields (filename,
        page, relevance) that are never shown to guests."""
        resp = self._openai.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL, input=[query])
        query_vector = resp.data[0].embedding

        result = self._collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        retrieved = [
            {
                "text": doc,
                "source": official_title(meta.get("filename", "")),
                "source_document": meta.get("filename", ""),
                "page": meta["page"],
                "relevance": round(1 - dist, 4),
            }
            for doc, meta, dist in zip(
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
            )
        ]
        accepted = filter_by_relevance(retrieved)

        best = max((c["relevance"] for c in retrieved), default=0.0)
        log.info("rag_search retrieved=%d accepted=%d best=%.3f "
                 "threshold=%.2f sources=%s fallback=%s",
                 len(retrieved), len(accepted), best, RAG_MIN_RELEVANCE,
                 sorted({c["source_document"] for c in accepted}),
                 not accepted)
        return accepted
