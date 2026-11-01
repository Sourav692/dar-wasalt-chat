import numpy as np
import pytest
from rank_bm25 import BM25Okapi

from app.constraints import Filters, english_terms, parse
from app.retrieval import Index, lexical_text, search, tokenize


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "3-bed apartment for rent in Riyadh under 1M",
            Filters("Riyadh", "rent", "Apartment", bedrooms=3, max_price=1e6),
        ),
        (
            "villas for sale in Al Khobar between 2 and 3.5 million SAR",
            Filters("Khobar", "sale", "Villa", min_price=2e6, max_price=3.5e6),
        ),
        ("between 500 and 800k", Filters(min_price=5e5, max_price=8e5)),
        ("4+ bedroom villa Jeddah", Filters("Jeddah", property_type="Villa", min_bedrooms=4)),
        ("فلل للإيجار بجدة أقل من 200 ألف", Filters("Jeddah", "rent", "Villa", max_price=2e5)),
        ("٣ غرف نوم في الرياض", Filters("Riyadh", bedrooms=3)),
        ("What is Wasalt Business?", Filters()),  # "Wasalt" must not read as "sale"
        ("current rental yields", Filters(purpose="rent")),
    ],
)
def test_parse(query, expected):
    assert parse(query) == expected


def test_unknown_values_never_match_a_set_filter():
    f = Filters(max_price=1e6, bedrooms=3)
    assert f.matches({"price": 900_000, "bedrooms": 3})
    assert not f.matches({"bedrooms": 3})  # price unknown
    assert not f.matches({"price": 900_000})  # bedrooms unknown
    assert not Filters()  # empty filters are falsy


def test_arabic_names_map_to_english():
    assert english_terms("ما هي منصة وصلت؟") == ["Wasalt"]


def chunk(i, kind="page", url=None, text="", meta=None):
    return {
        "id": f"c{i}",
        "doc_id": f"d{i}",
        "kind": kind,
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
    return Index(chunks, vectors, bm25, {"chunks": 3})


def test_filters_drop_contradicting_listings_but_keep_pages(index):
    r = search(index, "villa for sale in Jeddah", query_vec=np.ones(3, dtype=np.float32))
    ids = [c["id"] for c in r.chunks]
    assert "c2" in ids and "c1" not in ids and "c0" in ids
    assert not r.relaxed


def test_filters_relax_when_no_listing_matches(index):
    r = search(index, "villa in Dammam", query_vec=np.ones(3, dtype=np.float32))
    assert r.relaxed and r.filters.city == "Dammam"
    assert {"c1", "c2"} <= {c["id"] for c in r.chunks}


def test_one_chunk_per_page(index):
    index.chunks[1]["doc_id"] = index.chunks[2]["doc_id"] = "same"
    r = search(index, "villa", query_vec=np.ones(3, dtype=np.float32))
    assert sum(c["doc_id"] == "same" for c in r.chunks) == 1
