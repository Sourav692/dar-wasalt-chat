from ingest.build_index import CHUNK_WORDS, OVERLAP_WORDS, build_chunks, chunk_text


def words(n: int, tag: str) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def test_chunks_stay_near_size_and_overlap():
    text = "\n".join(words(10, f"l{j}w") for j in range(30))  # 300 words, 10 per line
    chunks = chunk_text(text)
    assert len(chunks) >= 300 // CHUNK_WORDS
    for _, body in chunks:
        assert len(body.split()) <= CHUNK_WORDS + OVERLAP_WORDS
    first_last_line = chunks[0][1].splitlines()[-1]
    assert chunks[1][1].startswith(first_last_line)  # overlap carried forward


def test_heading_starts_new_chunk_and_sets_section():
    text = f"## Location\n{words(50, 'a')}\n## Payment plan\n{words(20, 'b')}"
    chunks = chunk_text(text)
    assert [s for s, _ in chunks] == ["Location", "Payment plan"]
    assert chunks[1][1].startswith("## Payment plan")  # no overlap across a heading


def test_long_line_is_split():
    chunks = chunk_text(words(CHUNK_WORDS * 2 + 5, "x"))
    assert all(len(body.split()) <= CHUNK_WORDS + OVERLAP_WORDS for _, body in chunks)
    assert "x0" in chunks[0][1] and f"x{CHUNK_WORDS * 2 + 4}" in chunks[-1][1]


def test_listing_is_one_chunk_with_city_alias():
    doc = {
        "id": "w1",
        "site": "wasalt",
        "kind": "listing",
        "url": "https://wasalt.sa/en/property/sale/x-1",
        "title": "Villa for sale in Aldammam",
        "text": "Villa for sale in Aldammam with 4 bedrooms.",
        "snapshot_date": "2026-11-01",
        "meta": {"city": "Aldammam", "purpose": "sale"},
    }
    [chunk] = build_chunks([doc])
    assert chunk["meta"]["city"] == "Dammam"
    assert "Dammam" in chunk["text"] and "Aldammam" not in chunk["title"]


def test_tokenize_matches_plurals_possessives_and_arabic_variants():
    from app.retrieval import tokenize

    assert tokenize("Dar Global's 3-bed Villas, Riyadh") == [
        "dar",
        "global",
        "3",
        "bed",
        "villa",
        "riyadh",
    ]
    assert tokenize("bedrooms") == tokenize("Bedroom")
    assert tokenize("business") == ["business"]  # "ss" endings kept
    assert tokenize("مَدِينَة") == tokenize("مدينه")  # diacritics and taa marbuta folded
