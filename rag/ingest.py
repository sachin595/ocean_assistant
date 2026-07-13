"""Build the knowledge-base vector index: load PDFs, chunk by section,
embed, and store in ChromaDB. Run once before starting the assistant."""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb
import pdfplumber
from openai import OpenAI

from config import (CHROMA_COLLECTION, CHROMA_DIR, KB_DIR,
                    OPENAI_EMBEDDING_MODEL, require_openai_key)
from rag.source_names import SOURCE_NAMES


MAX_PROSE_CHARS = 2800   # ~700 tokens — small docs, section-sized chunks
OVERLAP_CHARS = 300
EMBED_BATCH = 128


@dataclass
class Chunk:
    text: str
    source: str      # human-friendly doc name
    filename: str
    page: int        # 1-based
    chunk_type: str  # "table" | "text"


# ── Extraction ─────────────────────────────────────────────────────────────

def _table_to_markdown(table: list[list]) -> str:
    """Render pdfplumber's list-of-rows as a markdown table."""
    def clean(cell) -> str:
        return " ".join(str(cell).split()) if cell else ""

    rows = [[clean(c) for c in row] for row in table if any(row)]
    if not rows:
        return ""
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _prose_outside_tables(page) -> str:
    """Page text with characters inside any detected table bbox removed,
    so table content isn't indexed twice (once structured, once flattened)."""
    bboxes = [t.bbox for t in page.find_tables()]
    if not bboxes:
        return page.extract_text() or ""

    def outside(obj) -> bool:
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= cx <= x1 and top <= cy <= bottom
                       for (x0, top, x1, bottom) in bboxes)

    return page.filter(outside).extract_text() or ""


def _split_prose(text: str) -> list[str]:
    """Greedy paragraph packing into ~MAX_PROSE_CHARS chunks with overlap."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if current and len(current) + len(para) + 1 > MAX_PROSE_CHARS:
            chunks.append(current)
            current = current[-OVERLAP_CHARS:] + "\n" + para  # soft overlap
        else:
            current = f"{current}\n{para}" if current else para
    if current.strip():
        chunks.append(current)
    return chunks


def extract_chunks(kb_dir: Path = KB_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for pdf_path in sorted(kb_dir.glob("*.pdf")):
        source = SOURCE_NAMES.get(pdf_path.name, pdf_path.stem)
        with pdfplumber.open(pdf_path) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                # Tables — one chunk each, prefixed with doc context so the
                # embedding carries topical meaning, not just cell values.
                for table in page.extract_tables():
                    md = _table_to_markdown(table)
                    if md:
                        chunks.append(Chunk(
                            text=f"From: {source} (page {page_no})\n\n{md}",
                            source=source, filename=pdf_path.name,
                            page=page_no, chunk_type="table"))
                # Prose — everything outside table bboxes.
                for piece in _split_prose(_prose_outside_tables(page)):
                    chunks.append(Chunk(
                        text=f"From: {source} (page {page_no})\n\n{piece}",
                        source=source, filename=pdf_path.name,
                        page=page_no, chunk_type="text"))
    return chunks


# ── Embedding + indexing ───────────────────────────────────────────────────

def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Batched OpenAI embedding calls; order-preserving."""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        resp = client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=batch)
        vectors.extend(item.embedding for item in resp.data)
    return vectors


def build_index() -> int:
    require_openai_key()
    client = OpenAI()  # reads OPENAI_API_KEY from the environment

    chunks = extract_chunks()
    tables = sum(1 for c in chunks if c.chunk_type == "table")
    print(f"Extracted {len(chunks)} chunks "
          f"({tables} tables, {len(chunks) - tables} prose) from 12 PDFs.")

    print(f"Embedding with {OPENAI_EMBEDDING_MODEL} ...")
    vectors = embed_texts(client, [c.text for c in chunks])

    chroma = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        chroma.delete_collection(CHROMA_COLLECTION)  # idempotent rebuild
    except Exception:
        pass
    collection = chroma.create_collection(
        CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"})

    collection.add(
        ids=[f"chunk-{i:04d}" for i in range(len(chunks))],
        documents=[c.text for c in chunks],
        embeddings=vectors,
        metadatas=[{"source": c.source, "filename": c.filename,
                    "page": c.page, "chunk_type": c.chunk_type}
                   for c in chunks],
    )
    print(f"Indexed {collection.count()} chunks into ChromaDB at {CHROMA_DIR}.")
    return collection.count()


if __name__ == "__main__":
    build_index()
