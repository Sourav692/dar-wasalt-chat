"""Build the retrieval index from data/docs.jsonl.

Writes index/chunks.jsonl, index/vectors.npy (L2-normalised float32) and index/manifest.json.
BM25 is rebuilt from chunks.jsonl when the app loads (milliseconds for ~2k chunks), so no
pickle is stored or downloaded.

Usage: uv run python -m ingest.build_index [--upload gs://bucket/prefix]
"""

import argparse
import collections
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "data" / "docs.jsonl"
OUT = ROOT / "index"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# The model truncates input at 128 word pieces, so chunks stay near 80 words (+ title prefix).
CHUNK_WORDS = 80
OVERLAP_WORDS = 12
HEADING = re.compile(r"^#{1,6}\s+(.+)")
# Wasalt publishes some city names in long or transliterated form; queries use the short one.
CITY_ALIASES = {"Aldammam": "Dammam", "Makkah Al Mukarramah": "Makkah"}


def split_long(line: str) -> list[str]:
    words = line.split()
    return [" ".join(words[i : i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]


def chunk_text(text: str) -> list[tuple[str, str]]:
    """Group a page's lines into (section, text) chunks of about CHUNK_WORDS words.

    A heading starts a new chunk once the current one is at least half full, and each
    chunk repeats the last ~OVERLAP_WORDS words of lines from the one before.
    """
    lines = [p for line in text.splitlines() if line.strip() for p in split_long(line)]
    chunks, cur, count, section = [], [], 0, ""
    cur_section = ""
    for line in lines:
        words = len(line.split())
        heading = HEADING.match(line)
        if cur and (count + words > CHUNK_WORDS or (heading and count >= CHUNK_WORDS // 2)):
            chunks.append((cur_section, "\n".join(cur)))
            keep, kept = [], 0
            if not heading:  # carry overlap only when the text runs on, not across a heading
                for prev in reversed(cur):
                    kept += len(prev.split())
                    if kept > OVERLAP_WORDS:
                        break
                    keep.insert(0, prev)
            cur, count, cur_section = keep, sum(len(k.split()) for k in keep), section
        if heading:
            section = heading.group(1).strip()
            if not cur:
                cur_section = section
        cur.append(line)
        count += words
    if cur:
        chunks.append((cur_section, "\n".join(cur)))
    return chunks


def canonical_listing(doc: dict) -> dict:
    city = doc["meta"].get("city", "")
    alias = CITY_ALIASES.get(city)
    if not alias:
        return doc
    return doc | {
        "title": doc["title"].replace(city, alias),
        "text": doc["text"].replace(city, alias),
        "meta": doc["meta"] | {"city": alias},
    }


def build_chunks(docs: list[dict]) -> list[dict]:
    chunks = []
    for doc in docs:
        base = {k: doc[k] for k in ("site", "kind", "url", "title", "snapshot_date")}
        if doc["kind"] == "listing":
            doc = canonical_listing(doc)
            chunks.append(
                base
                | {
                    "id": doc["id"],
                    "doc_id": doc["id"],
                    "title": doc["title"],
                    "section": "",
                    "text": doc["text"],
                    "meta": doc["meta"],
                }
            )
            continue
        for i, (section, text) in enumerate(chunk_text(doc["text"])):
            chunks.append(
                base
                | {
                    "id": f"{doc['id']}#{i}",
                    "doc_id": doc["id"],
                    "section": section,
                    "text": text,
                    "meta": {},
                }
            )
    return chunks


def embed_input(chunk: dict) -> str:
    head = " - ".join(p for p in (chunk["title"], chunk["section"]) if p)
    return f"{head}\n{chunk['text']}"


def main(upload: str | None) -> None:
    from fastembed import TextEmbedding

    raw = DOCS.read_bytes()
    docs = [json.loads(line) for line in raw.decode().splitlines()]
    chunks = build_chunks(docs)

    start = time.monotonic()
    model = TextEmbedding(MODEL)
    vectors = np.array(list(model.embed([embed_input(c) for c in chunks], batch_size=64)))
    vectors = (vectors / np.linalg.norm(vectors, axis=1, keepdims=True)).astype(np.float32)

    OUT.mkdir(exist_ok=True)
    (OUT / "chunks.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks)
    )
    np.save(OUT / "vectors.npy", vectors)
    manifest = {
        "model": MODEL,
        "dim": int(vectors.shape[1]),
        "docs": len(docs),
        "chunks": len(chunks),
        "by_kind": dict(collections.Counter(f"{c['site']}/{c['kind']}" for c in chunks)),
        "chunk_words": CHUNK_WORDS,
        "overlap_words": OVERLAP_WORDS,
        "docs_sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_dates": sorted({d["snapshot_date"] for d in docs}),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2), f"\nembedded in {time.monotonic() - start:.1f}s")

    if upload:
        dest = f"{upload.rstrip('/')}/{manifest['docs_sha256'][:12]}/"
        files = [str(OUT / name) for name in ("chunks.jsonl", "vectors.npy", "manifest.json")]
        subprocess.run(["gcloud", "storage", "cp", *files, dest], check=True)  # noqa: S603,S607
        print(f"uploaded -> {dest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", help="gs:// prefix; files go under <prefix>/<docs hash>/")
    main(parser.parse_args().upload)
