"""
Semantic embeddings — Gemini text-embedding-004.

Used for:
  - Deduplication: is this parsed task the same as an existing sheet row?
  - Backlog matching: does this task correspond to a backlog item?
  - AI context: find the top-N most relevant existing tasks for the prompt.
"""

import logging
import math
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "text-embedding-004:embedContent"
)
_BATCH_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "text-embedding-004:batchEmbedContents"
)

# Simple in-memory cache so the same task text isn't re-embedded on every call.
# Key: text string  →  Value: embedding vector
_MAX_CACHE = 500
_cache: dict[str, list[float]] = {}


def _cache_put(text: str, vector: list[float]) -> None:
    if len(_cache) >= _MAX_CACHE:
        # Evict oldest half when full
        keys = list(_cache.keys())
        for k in keys[: _MAX_CACHE // 2]:
            del _cache[k]
    _cache[text] = vector


# ──────────────────────────────────────────────
# Core embedding calls
# ──────────────────────────────────────────────


async def embed_text(text: str) -> list[float] | None:
    """Return embedding vector for a single text. Returns None on failure."""
    if not text or not settings.GEMINI_API_KEY:
        return None

    text = text.strip()
    if text in _cache:
        return _cache[text]

    try:
        payload = {
            "model": "models/text-embedding-004",
            "content": {"parts": [{"text": text}]},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_EMBED_URL}?key={settings.GEMINI_API_KEY}",
                json=payload,
            )
            resp.raise_for_status()
            vector = resp.json()["embedding"]["values"]
            _cache_put(text, vector)
            return vector
    except Exception as e:
        logger.warning("embed_text failed for '%s...': %s", text[:40], e)
        return None


async def embed_texts(texts: list[str]) -> dict[str, list[float]]:
    """
    Batch-embed a list of texts in a single API call.
    Returns a dict mapping each text → vector.
    Texts already in cache are skipped from the API call.
    """
    if not settings.GEMINI_API_KEY:
        return {}

    results: dict[str, list[float]] = {}
    to_fetch: list[str] = []

    for text in texts:
        text = text.strip()
        if not text:
            continue
        if text in _cache:
            results[text] = _cache[text]
        else:
            to_fetch.append(text)

    if not to_fetch:
        return results

    try:
        payload = {
            "requests": [
                {
                    "model": "models/text-embedding-004",
                    "content": {"parts": [{"text": t}]},
                }
                for t in to_fetch
            ]
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_BATCH_URL}?key={settings.GEMINI_API_KEY}",
                json=payload,
            )
            resp.raise_for_status()
            embeddings = resp.json().get("embeddings", [])

        for text, emb in zip(to_fetch, embeddings):
            vector = emb.get("values", [])
            if vector:
                _cache_put(text, vector)
                results[text] = vector

    except Exception as e:
        logger.warning("embed_texts batch failed: %s — falling back to singles", e)
        for text in to_fetch:
            vec = await embed_text(text)
            if vec:
                results[text] = vec

    return results


# ──────────────────────────────────────────────
# Similarity
# ──────────────────────────────────────────────


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0.0–1.0."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ──────────────────────────────────────────────
# High-level helpers
# ──────────────────────────────────────────────


async def find_best_match(
    query: str,
    candidates: list[str],
    threshold: float = 0.82,
) -> tuple[str | None, float]:
    """
    Find the most semantically similar candidate to query.
    Returns (best_match, score) or (None, score) if nothing clears the threshold.

    Used for:
      - Dedup: query = parsed task name, candidates = existing sheet row names
      - Backlog: query = parsed task name, candidates = backlog item names
    """
    if not query or not candidates:
        return None, 0.0

    all_texts = [query] + candidates
    vectors = await embed_texts(all_texts)

    query_vec = vectors.get(query.strip())
    if query_vec is None:
        return None, 0.0

    best_match: str | None = None
    best_score = 0.0

    for candidate in candidates:
        candidate = candidate.strip()
        cand_vec = vectors.get(candidate)
        if cand_vec is None:
            continue
        score = cosine_similarity(query_vec, cand_vec)
        if score > best_score:
            best_score = score
            best_match = candidate

    if best_score >= threshold:
        return best_match, best_score
    return None, best_score


async def find_top_k(
    query: str,
    candidates: list[str],
    k: int = 5,
) -> list[tuple[str, float]]:
    """
    Return the top-k most similar candidates with their scores, sorted descending.
    Used to build a focused AI context (most relevant existing tasks).
    """
    if not query or not candidates:
        return []

    all_texts = [query] + candidates
    vectors = await embed_texts(all_texts)

    query_vec = vectors.get(query.strip())
    if query_vec is None:
        return []

    scored = []
    for candidate in candidates:
        candidate = candidate.strip()
        cand_vec = vectors.get(candidate)
        if cand_vec is None:
            continue
        score = cosine_similarity(query_vec, cand_vec)
        scored.append((candidate, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
