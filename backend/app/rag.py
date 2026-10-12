"""RAG over the policy corpus (knowledge/). The corpus is the single source of truth for business policy:
front-matter (structured facts, e.g. required documents) goes to rag.documents.metadata,
prose sections go to rag.chunks (vector + full-text)."""
import hashlib
import os
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import structlog
import yaml

from app import db
from app.settings import ROOT

log = structlog.get_logger()
KNOWLEDGE = ROOT / "knowledge"
MIN_RELEVANCE = 0.30  # below this the best hit is "not really about the question" -> uncertainty signal


@lru_cache
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))


@lru_cache
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2", cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))


def embed(texts: list[str]) -> list[np.ndarray]:
    return list(_embedder().embed(texts))


# ---------------------------------------------------------------- ingestion
def parse(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    raw = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not m:
        raise ValueError(f"{path}: missing front-matter")
    meta, body = yaml.safe_load(m.group(1)), m.group(2)
    sections, current, buf = [], meta["title"], []
    for line in body.splitlines():
        if line.startswith("## "):
            if "".join(buf).strip():
                sections.append((current, "\n".join(buf).strip()))
            current, buf = line[3:].strip(), []
        elif not line.startswith("# "):
            buf.append(line)
    if "".join(buf).strip():
        sections.append((current, "\n".join(buf).strip()))
    # ponytail: one chunk per "##" section; split long sections by paragraph when docs get big.
    return meta, sections


def ingest(root: Path = KNOWLEDGE) -> dict:
    """Idempotent sync: unchanged files skipped (sha256), changed files re-embedded, deleted files removed."""
    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    seen = []
    for path in sorted(root.rglob("*.md")):
        rel = str(path.relative_to(root))
        seen.append(rel)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = db.fetch_one("SELECT id, sha256 FROM rag.documents WHERE source_path = %s", rel)
        if existing and existing["sha256"] == sha:
            stats["unchanged"] += 1
            continue
        meta, sections = parse(path)
        texts = [f"{meta['title']} - {sec}\n{content}" for sec, content in sections]
        vectors = embed(texts)
        with db.pool().connection() as c, c.transaction():
            c.execute("DELETE FROM rag.documents WHERE source_path = %s", (rel,))
            doc = c.execute(
                "INSERT INTO rag.documents (source_path, title, product_code, doc_type, metadata, sha256, effective_date)"
                " VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
                (rel, meta["title"], meta.get("product"), meta["doc_type"], _json(meta), sha, meta.get("effective_date")),
            ).fetchone()
            for i, ((sec, content), vec) in enumerate(zip(sections, vectors)):
                c.execute(
                    "INSERT INTO rag.chunks (document_id, chunk_index, section, content, metadata, embedding)"
                    " VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                    (doc["id"], i, sec, content, _json({"source": rel, "title": meta["title"]}), vec),
                )
        stats["updated" if existing else "added"] += 1
    gone = db.fetch("DELETE FROM rag.documents WHERE NOT (source_path = ANY(%s)) RETURNING id", seen)
    stats["removed"] = len(gone)
    log.info("rag.ingest", **stats)
    return stats


def _json(d: dict) -> str:
    import json

    return json.dumps(d, default=str)


# ---------------------------------------------------------------- retrieval
def search(query: str, product: str | None = None, k: int = 5) -> dict:
    """Hybrid retrieval: vector top-20 + full-text top-20 -> Reciprocal Rank Fusion -> cross-encoder rerank.
    Returns {"hits": [...], "low_relevance": bool}. low_relevance is an uncertainty signal for the supervisor."""
    where = "(d.product_code IS NULL OR d.product_code = %s)" if product else "TRUE"
    p = [product] if product else []
    base = (
        "SELECT ch.id, ch.section, ch.content, d.title, d.source_path, d.doc_type FROM rag.chunks ch"
        f" JOIN rag.documents d ON d.id = ch.document_id WHERE {where}"
    )
    candidates: dict = {}
    ranks: dict[str, float] = {}
    try:
        vec = embed([query])[0]
        for r, row in enumerate(db.fetch(base + " ORDER BY ch.embedding <=> %s LIMIT 20", *p, vec)):
            candidates[row["id"]] = row
            ranks[row["id"]] = ranks.get(row["id"], 0) + 1 / (60 + r)
    except Exception as e:  # vector path down -> full-text still answers (fail soft, but loudly)
        log.error("rag.vector_failed", error=str(e))
    fts = db.fetch(
        base + " AND ch.fts @@ websearch_to_tsquery('english', %s)"
        " ORDER BY ts_rank(ch.fts, websearch_to_tsquery('english', %s)) DESC LIMIT 20",
        *p, _or_query(query), _or_query(query),
    )
    for r, row in enumerate(fts):
        candidates[row["id"]] = row
        ranks[row["id"]] = ranks.get(row["id"], 0) + 1 / (60 + r)
    if not candidates:
        return {"hits": [], "low_relevance": True}
    fused = sorted(candidates.values(), key=lambda row: ranks[row["id"]], reverse=True)[:20]
    scores = list(_reranker().rerank(query, [f"{c['section']}\n{c['content']}" for c in fused]))
    ranked = sorted(zip(fused, scores), key=lambda x: x[1], reverse=True)[:k]
    hits = [
        {
            "source": c["source_path"],
            "title": c["title"],
            "section": c["section"],
            "content": c["content"],
            "relevance": round(1 / (1 + math.exp(-s)), 3),  # sigmoid of cross-encoder logit
        }
        for c, s in ranked
    ]
    return {"hits": hits, "low_relevance": hits[0]["relevance"] < MIN_RELEVANCE}


def _or_query(q: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]{3,}", q)]
    return " OR ".join(words) or q


def product_policy(product: str) -> dict | None:
    """Structured facts of a product, from the same RAG documents (single source of truth)."""
    return db.fetch_one(
        "SELECT title, source_path, metadata FROM rag.documents"
        " WHERE doc_type = 'product_policy' AND product_code = %s ORDER BY effective_date DESC LIMIT 1",
        product,
    )


def products() -> list[dict]:
    rows = db.fetch("SELECT product_code, metadata->>'label' AS label FROM rag.documents WHERE doc_type='product_policy'")
    return [{"code": r["product_code"], "label": r["label"]} for r in rows]


if __name__ == "__main__":
    print(ingest())
