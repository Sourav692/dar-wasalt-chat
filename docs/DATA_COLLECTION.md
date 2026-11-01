# Data collection

The chatbot answers from a one-time snapshot of public pages on darglobal.co.uk and wasalt.sa. Dar Global was crawled on 2026-10-31. Wasalt was captured by hand on 2026-11-01, because Cloudflare challenged the crawler on 19 of its 20 pages.

## What was collected

| Source | Pages fetched | Docs in corpus |
|---|---|---|
| Dar Global sitemap (`/sitemap.xml`, 477 URLs) | 250 | 233 pages |
| Wasalt `/en/corporate` (crawler) | 1 | 1 page |
| Wasalt hand-saved pages | 13 | 13 pages + 200 listings |
| **Total** | **264** | **447 docs** |

Dar Global: the sitemap was filtered to drop forms, payment, thank-you and campaign pages (`/pay-online`, `/bookYourUnit`, `/landing`, `/campaigns` and five more; see `crawler/seeds.yaml`), and blog posts were capped at 50. 17 of the 250 fetched pages did not become docs:

- 4 pages render a client-side error instead of content ("Application error: a client-side exception").
- `/careers` redirects to a third-party job board (`darglobal.thetalentpool.co.in`), which is off-scope.
- 12 pages had under 200 characters of real text: 8 as extracted, and 4 more once site-wide boilerplate was removed.

Wasalt: 10 category pages (apartments, villas and land, for sale and rent, across Riyadh, Jeddah, Dammam, Makkah, Madinah and Khobar), the home page, terms of use and privacy policy. `s/wasalt-business` and `s/masterplan` now redirect to the home page, so they add nothing. Six other legal pages in Wasalt's sitemap (e-wallet, auction, advertising terms) were not captured.

Listings are never fetched from their own pages. Each category page embeds about 30 listing cards as JSON in its `__NEXT_DATA__` script, and the extractor samples 20 per category, 200 in total. That is a sample of the catalogue, not all of it.

## How the crawler behaves

`crawler/crawl.py` drives real Chromium through Playwright.

- **robots.txt.** Read for every host before its first page and parsed with `protego`, because Python's `urllib.robotparser` ignores the `*` and `$` wildcards Dar Global uses. A 404 means everything is allowed. Any other non-200 response means the host is skipped entirely. All three hosts served a readable robots.txt, and every requested URL was allowed.
- **Identity.** The User-Agent ends with `DarWasaltResearchBot/0.1 (+https://github.com/Sourav692/dar-wasalt-chat)`, so site operators can see who is crawling and why.
- **Rate.** One page at a time per host, at least 5 seconds between requests to the same host, and a 3-second render wait. The full crawl (270 URLs across both sites) took 26 minutes.
- **Bot challenges.** A response counts as a challenge if its status is 403, 429 or 503, or if its title or opening text matches a known challenge phrase ("Performing security verification", "Just a moment", "Incapsula incident" and similar). A challenged page is logged as `skipped_challenge` and never retried. The crawler does not rotate identities, use proxies, use solver services or alter its fingerprint.
- **Scope.** Only `https` URLs on each site's allowlisted hosts are queued, and a page whose final URL lands off those hosts is discarded. The extractor checks again before anything enters the corpus.
- **Resumable.** Results append to `data/raw/manifest.jsonl`; a re-run skips URLs already fetched. `data/raw/crawl_report.json` holds the counts per outcome.

## Manual capture for Wasalt

Cloudflare challenged the crawler on all 10 category pages and on 9 static pages; only `/en/corporate` came through. A bot challenge tells us the site does not want automated access to those pages, so the crawler stopped there.

For the pages the chatbot needs, a person opened each one in their own Chrome session and passed any check themselves, at human speed. Once the 13 tabs were open, a browser-extension script saved each tab's HTML to `data/manual/wasalt/`, which is the same as File → Save Page As. The script did not open, reload or navigate any page. Each file starts with a `<!-- saved from url=... -->` comment, which the extractor uses to recover the source URL, and the date in the file name becomes the snapshot date. The steps are in `crawler/manual/README.md`.

## Extraction and personal data

`crawler/extract.py` turns raw HTML into `data/docs.jsonl`, one JSON object per doc: `id, site, kind, url, title, text, snapshot_date, meta`.

- Page text comes from trafilatura as markdown, keeping headings for chunking. Lines repeated on 10 or more pages (navigation, footers, cookie banners) are removed as boilerplate.
- Wasalt listing cards contain personal data: owner and agent names, phone and WhatsApp numbers, and REGA licence numbers. `clean_listing` reads a fixed allowlist of fields: purpose, city, district, territory, property type, price, currency, rent period, bedrooms and area. The listing text is generated from those fields only, never from the card's free-text title or description, which can hold contact details. Tests in `tests/test_extract.py` plant fake personal data in a card and assert none of it reaches the output.
- Prices are copied as published. A missing or zero price is left out rather than shown as free, and a rent with no stated period says "period unspecified" rather than guessing monthly or yearly. Most Wasalt rents fall in that group.

## Storage

Raw HTML and the corpus stay out of the public repo. `data/` is git-ignored, and the saved Wasalt HTML still contains the personal data described above, so it never leaves the local machine. Only the cleaned corpus is uploaded, to a private Google Cloud Storage bucket with public access prevention enforced:

```
gs://dar-wasalt-chat-22ytj-corpus/snapshots/2026-11-01/docs.jsonl
```

## Known gaps

- The snapshot does not update. Prices and availability are as of the snapshot date, and the bot says so.
- 200 sampled listings, not the full Wasalt catalogue.
- Wasalt city labels come through as published ("Aldammam", "Makkah Al Mukarramah"). Retrieval adds aliases so that "Dammam" and "Makkah" match.
- Six Wasalt legal pages and the four Dar Global pages that fail to render are missing.

## Reproduce

```bash
make install                                   # uv sync + playwright install chromium
uv run python -m crawler.crawl                 # writes data/raw/ and crawl_report.json
# save challenged pages by hand: crawler/manual/README.md
uv run python -m crawler.extract               # writes data/docs.jsonl
```
