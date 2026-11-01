import numpy as np
import pytest
from rank_bm25 import BM25Okapi

from app.retrieval import Index, lexical_text, tokenize


def chunk(i, kind="page", url=None, text="", meta=None):
    return {
        "id": f"c{i}",
        "doc_id": f"d{i}",
        "kind": kind,
        "site": "wasalt" if kind == "listing" else "darglobal",
        "snapshot_date": "2026-11-01",
        "url": url or f"https://x.test/p{i}",
        "title": "",
        "text": text,
        "meta": meta or {},
    }


@pytest.fixture
def index():
    chunks = [
        chunk(0, text="Riyadh villa market overview and buying guide"),
        chunk(
            1,
            "listing",
            text="Villa for sale in Riyadh",
            meta={"city": "Riyadh", "purpose": "sale", "property_type": "Villa", "price": 900_000},
        ),
        chunk(
            2,
            "listing",
            text="Villa for sale in Jeddah",
            meta={"city": "Jeddah", "purpose": "sale", "property_type": "Villa", "price": 800_000},
        ),
    ]
    vectors = np.eye(3, dtype=np.float32)
    bm25 = BM25Okapi([tokenize(lexical_text(c)) for c in chunks])
    return Index(
        chunks,
        vectors,
        bm25,
        {"chunks": 3, "docs_sha256": "0" * 64, "snapshot_dates": ["2026-11-01"]},
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
