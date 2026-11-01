"""Load the index built by ingest/build_index.py. Search (BM25 + dense + RRF) lands in Phase 3."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

INDEX_DIR = Path(__file__).resolve().parent.parent / "index"
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


def load_index(path: Path = INDEX_DIR) -> Index:
    chunks = [json.loads(line) for line in (path / "chunks.jsonl").read_text().splitlines()]
    vectors = np.load(path / "vectors.npy")
    manifest = json.loads((path / "manifest.json").read_text())
    if not len(chunks) == len(vectors) == manifest["chunks"]:
        raise ValueError(
            f"index mismatch: {len(chunks)} chunks, {len(vectors)} vectors, "
            f"manifest says {manifest['chunks']}"
        )
    bm25 = BM25Okapi([tokenize(f"{c['title']} {c['text']}") for c in chunks])
    return Index(chunks, vectors, bm25, manifest)
