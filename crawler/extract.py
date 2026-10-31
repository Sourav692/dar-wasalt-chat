"""Raw HTML -> data/docs.jsonl.

Sources:
  * data/raw/manifest.jsonl entries with outcome "ok" (from crawler/crawl.py)
  * data/manual/<site>/*.html pages saved by hand from a normal browser (see crawler/manual/)

Each output line: {id, site, kind, url, title, text, snapshot_date, meta}
  kind = "page" for prose pages, "listing" for a Wasalt property card.

Usage: uv run python -m crawler.extract [--listing-cap 200]
"""

import argparse
import hashlib
import html as htmllib
import json
import math
import re
from pathlib import Path

import trafilatura
import yaml

from crawler.crawl import SEEDS, in_scope

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MANUAL = ROOT / "data" / "manual"
OUT = ROOT / "data" / "docs.jsonl"

MIN_PAGE_CHARS = 200
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
SAVED_FROM = re.compile(r"<!-- saved from url=\(\d+\)(\S+) -->")
CANONICAL = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"')
RENDER_FAILED = "Application error: a client-side exception"
BOILERPLATE_MIN_DOCS = 10  # a line repeated on this many pages is site chrome, not content
INLINE_TAGS = re.compile(r"</?(?:u|sup|sub|span|br)\b[^>]*>")
RULE_LINE = re.compile(r"^(?:\\_){3,}$")
# Consent-banner fragments; wording/line breaks vary, so exact-repeat counting misses them.
BANNER_LINE = re.compile(
    r"^(?:We use cookies to enhance|We value our privacy|use of cookies\.?$)", re.I
)
# Dar Global project fact lists lose the label/value separator ("Property TypeSeafront").
FACT_LABEL = re.compile(
    r"^- (Property Type|Unit Type|Units|Status|Location|Stage|Number of Floors|Bedrooms"
    r"|Area ?\(sqm\))\s*(?=[^:\s])",
    re.M,
)


def doc_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:16]


def page_text(html: str) -> str:
    """Main content as markdown (headings kept for heading-aware chunking)."""
    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_formatting=True,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
    )
    return clean_text(text or "")


def unescape(text: str) -> str:
    """Unescape repeatedly: some titles arrive double-escaped (&amp;amp;)."""
    while (out := htmllib.unescape(text)) != text:
        text = out
    return text


def clean_text(text: str) -> str:
    text = unescape(INLINE_TAGS.sub("", text))
    text = FACT_LABEL.sub(r"- \1: ", text)
    lines = [
        ln
        for ln in text.splitlines()
        if not RULE_LINE.match(ln.strip()) and not BANNER_LINE.match(ln.strip())
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def drop_boilerplate(docs: list[dict]) -> list[dict]:
    """Remove lines that repeat across many pages (cookie banner, forms, taglines)."""
    counts: dict[str, int] = {}
    for d in docs:
        for ln in {ln.strip() for ln in d["text"].splitlines() if ln.strip()}:
            counts[ln] = counts.get(ln, 0) + 1
    for d in docs:
        kept = [
            ln for ln in d["text"].splitlines() if counts.get(ln.strip(), 0) < BOILERPLATE_MIN_DOCS
        ]
        d["text"] = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return [d for d in docs if len(d["text"]) >= MIN_PAGE_CHARS]


def next_data(html: str) -> dict:
    m = NEXT_DATA.search(html)
    return json.loads(m.group(1)).get("props", {}).get("pageProps", {}) if m else {}


def clean_listing(prop: dict, snapshot_date: str) -> dict | None:
    """Turn one raw Wasalt listing card into a listing doc, keeping only allowlisted fields.

    The raw card carries personal data (owner/agent names, phone and WhatsApp numbers,
    licence numbers, lead-contact text). None of that may reach the corpus.

    Return {"url", "title", "text", "meta"} or None to drop the card.
      url:  https://wasalt.sa/en/property/<propertyFor>/<propertyInfo.slug>
      meta: flat dict of scalars used for filtering, e.g. price, currency, purpose
            (sale/rent), city, district, property_type, bedrooms, area_sqm.
      text: one readable sentence or two built from meta, so BM25 and embeddings
            match queries like "3 bedroom apartment for rent in Riyadh".

    Useful raw paths (see tests/test_extract.py for a sample card):
      prop["propertyInfo"]: salePrice, expectedRent, rentFreq, currencyType, propertyFor,
                            city, district, territory, propertySubType, title, slug
      prop["attributes"]:   list of {"key": "noOfBedrooms" | "builtUpArea" | ..., "value": ...}
    """
    # Require enough context to identify and search a listing. Price is optional;
    # zero and missing prices mean unknown, not free. Never infer a rent period.
    if not isinstance(prop, dict) or not isinstance(prop.get("propertyInfo"), dict):
        return None
    info = prop["propertyInfo"]

    def label(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())
        return value if value and len(value) <= 100 else None

    def number(value: object, *, allow_zero: bool = False) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        if isinstance(value, str):
            value = value.strip()
            if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value):
                return None
            value = value.replace(",", "")
        try:
            result = float(value)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
            return None
        return int(result) if result.is_integer() else result

    slug = info.get("slug")
    purpose = label(info.get("propertyFor"))
    purpose = purpose.lower() if purpose else None
    city = label(info.get("city"))
    property_type = label(info.get("propertySubType"))
    if (
        not isinstance(slug, str)
        or not re.fullmatch(r"[\w]+(?:-[\w]+)*", slug)
        or purpose not in {"sale", "rent"}
        or not city
        or not property_type
    ):
        return None

    meta = {"purpose": purpose, "city": city, "property_type": property_type}
    for key in ("district", "territory"):
        if value := label(info.get(key)):
            meta[key] = value
    price = number(info.get("salePrice" if purpose == "sale" else "expectedRent"))
    if price is not None:
        meta["price"] = price
        currency = label(info.get("currencyType"))
        if currency and re.fullmatch(r"[A-Za-z]{3}", currency):
            meta["currency"] = currency.upper()
    if purpose == "rent":
        frequency = label(info.get("rentFreq"))
        periods = {
            "day": "daily",
            "daily": "daily",
            "week": "weekly",
            "weekly": "weekly",
            "month": "monthly",
            "monthly": "monthly",
            "year": "yearly",
            "yearly": "yearly",
            "annual": "yearly",
            "annually": "yearly",
        }
        if frequency and frequency.lower() in periods:
            meta["rent_frequency"] = periods[frequency.lower()]

    attributes = prop.get("attributes")
    for attr in attributes if isinstance(attributes, list) else []:
        if not isinstance(attr, dict):
            continue
        if attr.get("key") == "noOfBedrooms":
            bedrooms = number(attr.get("value"), allow_zero=True)
            if isinstance(bedrooms, int):
                meta.setdefault("bedrooms", bedrooms)
        elif attr.get("key") == "builtUpArea":
            unit = label(attr.get("unit"))
            area = number(attr.get("value"))
            if area is not None and (unit is None or unit.lower() in {"sqm", "m2", "m²"}):
                meta.setdefault("area_sqm", area)

    # Build prose from structured fields; raw titles and descriptions can contain contacts.
    location = ", ".join(meta[key] for key in ("district", "city") if key in meta)
    title = f"{property_type} for {purpose} in {location}"
    details = []
    if "bedrooms" in meta:
        details.append(f"{meta['bedrooms']} bedrooms")
    if "area_sqm" in meta:
        details.append(f"{meta['area_sqm']} sqm")
    text = title + (f" with {', '.join(details)}" if details else "") + "."
    if price is not None:
        amount = f"{meta.get('currency', '')} {price}".strip()
        period = meta.get("rent_frequency", "period unspecified")
        text += f" Asking price: {amount}." if purpose == "sale" else f" Rent: {amount} ({period})."
    return {
        "url": f"https://wasalt.sa/en/property/{purpose}/{slug}",
        "title": title,
        "text": text,
        "meta": meta,
    }


def wasalt_category_docs(html: str, url: str, snapshot: str, cap: int) -> list[dict]:
    """A Wasalt category page yields its SEO overview text plus up to `cap` listing docs."""
    props = next_data(html)
    docs = []
    para = trafilatura.extract(props.get("para") or "", output_format="markdown") or ""
    if len(para) >= MIN_PAGE_CHARS:
        title = (props.get("metaData") or {}).get("title") or url.rsplit("/", 1)[-1]
        docs.append(make_doc("wasalt", "page", url, title, para, snapshot))
    cards = ((props.get("searchResult") or {}).get("properties")) or []
    for card in cards:
        if sum(d["kind"] == "listing" for d in docs) >= cap:
            break
        item = clean_listing(card, snapshot)
        if item:
            docs.append(
                make_doc("wasalt", "listing", item["url"], item["title"], item["text"], snapshot)
                | {"meta": item["meta"]}
            )
    return docs


def make_doc(site: str, kind: str, url: str, title: str, text: str, snapshot: str) -> dict:
    return {
        "id": doc_id(site, kind, url),
        "site": site,
        "kind": kind,
        "url": url,
        "title": unescape(" ".join(title.split())),
        "text": text,
        "snapshot_date": snapshot,
        "meta": {},
    }


def iter_sources():
    """Yield (site, url, html, snapshot_date) for crawled and hand-saved pages.

    Both the requested URL and the final URL (after redirects) must be on the site's
    allowlisted hosts; anything else is dropped.
    """
    hosts = {k: v["hosts"] for k, v in yaml.safe_load(SEEDS.read_text())["sites"].items()}
    manifest = RAW / "manifest.jsonl"
    if manifest.exists():
        latest = {}
        for line in manifest.read_text().splitlines():
            r = json.loads(line)
            latest[r["url"]] = r
        for r in latest.values():
            if r["outcome"] != "ok":
                continue
            if not in_scope(r.get("final_url") or r["url"], hosts[r["site"]]):
                print(f"[extract] off-scope redirect, skipped: {r['url']} -> {r['final_url']}")
                continue
            html = (ROOT / r["path"]).read_text(encoding="utf-8")
            yield r["site"], r["url"], html, r["fetched_at"][:10]
    for f in sorted(MANUAL.glob("*/*.html")):
        html = f.read_text(encoding="utf-8", errors="ignore")
        m = SAVED_FROM.search(html[:2000]) or CANONICAL.search(html)
        if not m:
            print(f"[manual] {f}: no source URL (saved-from comment or canonical link); skipped")
            continue
        if f.parent.name not in hosts or not in_scope(m.group(1), hosts[f.parent.name]):
            print(f"[manual] {f}: URL {m.group(1)} not on {f.parent.name} hosts; skipped")
            continue
        snapshot = re.search(r"\d{4}-\d{2}-\d{2}", f.stem)
        yield f.parent.name, m.group(1), html, snapshot.group(0) if snapshot else "unknown"


def main(listing_cap: int) -> None:
    sources = list(iter_sources())
    n_cat = sum(1 for s, _u, h, _ in sources if s == "wasalt" and "searchResult" in h)
    per_cat = math.ceil(listing_cap / max(n_cat, 1))  # stratify the sample across categories
    docs, seen = [], set()
    for site, url, html, snapshot in sources:
        if site == "wasalt" and "searchResult" in html:
            new = wasalt_category_docs(html, url, snapshot, per_cat)
        else:
            text = page_text(html)
            if RENDER_FAILED in text:
                print(f"[extract] render failed, skipped: {url}")
                continue
            title = re.search(r"<title>(.*?)</title>", html, re.S)
            new = (
                [make_doc(site, "page", url, title.group(1) if title else url, text, snapshot)]
                if len(text) >= MIN_PAGE_CHARS
                else []
            )
        for d in new:
            if d["id"] not in seen:
                seen.add(d["id"])
                docs.append(d)
    listings = [d for d in docs if d["kind"] == "listing"][:listing_cap]
    docs = drop_boilerplate([d for d in docs if d["kind"] == "page"]) + listings
    OUT.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs))
    by = {}
    for d in docs:
        by[f"{d['site']}/{d['kind']}"] = by.get(f"{d['site']}/{d['kind']}", 0) + 1
    print(json.dumps(by, indent=2), f"\n-> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing-cap", type=int, default=200)
    main(ap.parse_args().listing_cap)
