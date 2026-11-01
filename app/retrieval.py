"""Hybrid retrieval over the index built by ingest/build_index.py.

BM25 and dense rankings are fused with reciprocal rank fusion (RRF). Listing filters from
app.constraints are applied before ranking, so a filtered query still fills its pool.
"""

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from rank_bm25 import BM25Okapi

from app.constraints import Filters, english_terms, parse

INDEX_DIR = Path(__file__).resolve().parent.parent / "index"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
POOL = 30  # candidates taken from each ranker
RRF_K = 60
TOP_K = 6
PER_DOC = 1  # one chunk per page keeps the top 6 diverse; prompt.py adds neighbours
TOKEN = re.compile(r"\w+")
ARABIC_MARKS = re.compile(r"[ً-ْـ]")  # diacritics and tatweel
ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def tokenize(text: str) -> list[str]:
    """Split text into BM25 tokens. The same function runs on chunks and on queries.

    Lowercase, Arabic letter variants folded, single letters dropped ("Global's" -> "global"),
    and a crude plural strip so "bedrooms" matches "bedroom" and "villas" matches "villa".
    """
    text = ARABIC_MARKS.sub("", text.lower()).translate(ARABIC_FOLD)
    tokens = []
    for tok in TOKEN.findall(text):
        if len(tok) == 1 and not tok.isdigit():
            continue
        # ponytail: suffix strip, not a stemmer; swap in snowball if eval shows misses
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        tokens.append(tok)
    return tokens


@dataclass
class Index:
    chunks: list[dict]
    vectors: np.ndarray
    bm25: BM25Okapi
    manifest: dict
    is_listing: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.is_listing = np.array([c["kind"] == "listing" for c in self.chunks])


@dataclass
class Result:
    chunks: list[dict]
    filters: Filters
    relaxed: bool  # filters matched no listing and were dropped


def lexical_text(chunk: dict) -> str:
    """BM25 sees the title twice (a cheap field boost) and the URL path words, which often
    name the page better than its body ("/become-a-broker", "/get-in-touch")."""
    path = urlsplit(chunk["url"]).path.replace("-", " ").replace("/", " ")
    return f"{chunk['title']} {chunk['title']} {path} {chunk['text']}"


def load_index(path: Path = INDEX_DIR) -> Index:
    chunks = [json.loads(line) for line in (path / "chunks.jsonl").read_text().splitlines()]
    vectors = np.load(path / "vectors.npy")
    manifest = json.loads((path / "manifest.json").read_text())
    if not len(chunks) == len(vectors) == manifest["chunks"]:
        raise ValueError(
            f"index mismatch: {len(chunks)} chunks, {len(vectors)} vectors, "
            f"manifest says {manifest['chunks']}"
        )
    bm25 = BM25Okapi([tokenize(lexical_text(c)) for c in chunks])
    return Index(chunks, vectors, bm25, manifest)


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(MODEL)


def embed_query(text: str) -> np.ndarray:
    vec = next(iter(_embedder().embed([text])))
    return (vec / np.linalg.norm(vec)).astype(np.float32)


def rrf(rankings: list[np.ndarray], n: int) -> np.ndarray:
    fused = np.zeros(n)
    for ranking in rankings:
        fused[ranking] += 1.0 / (RRF_K + 1 + np.arange(len(ranking)))
    return fused


def search(
    index: Index,
    query: str,
    query_vec: np.ndarray | None = None,
    filters: Filters | None = None,
    k: int = TOP_K,
) -> Result:
    filters = parse(query) if filters is None else filters
    query_vec = embed_query(query) if query_vec is None else query_vec
    allowed = np.ones(len(index.chunks), dtype=bool)
    relaxed = False
    if filters:
        ok = np.array([filters.matches(c["meta"]) for c in index.chunks])
        if (index.is_listing & ok).any():
            allowed = ~index.is_listing | ok
        else:
            relaxed = True

    # Parsed filters add English terms, so an Arabic listing query still gets a BM25 vote.
    terms = [filters.city, filters.purpose, filters.property_type]
    lexical = " ".join([query, *(t for t in terms if t), *english_terms(query)])
    bm25 = np.where(allowed, index.bm25.get_scores(tokenize(lexical)), -np.inf)
    dense = np.where(allowed, index.vectors @ query_vec, -np.inf)
    bm25_top = np.argsort(-bm25)[:POOL]
    bm25_top = bm25_top[bm25[bm25_top] > 0]  # no shared terms (e.g. Arabic query): no vote
    dense_top = np.argsort(-dense)[:POOL]
    dense_top = dense_top[np.isfinite(dense[dense_top])]  # filtered-out chunks get no vote
    fused = rrf([bm25_top, dense_top], len(index.chunks))

    picked, per_doc = [], {}
    for i in np.argsort(-fused):
        if fused[i] == 0 or len(picked) == k:
            break
        doc = index.chunks[i]["doc_id"]
        if per_doc.get(doc, 0) < PER_DOC:
            per_doc[doc] = per_doc.get(doc, 0) + 1
            picked.append(index.chunks[i] | {"score": round(float(fused[i]), 5)})
    return Result(picked, filters, relaxed)
