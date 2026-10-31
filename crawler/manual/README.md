# Manual capture

The crawler skips any page that answers with a bot challenge (Cloudflare "Performing security verification", Imperva/Incapsula, HTTP 403/429/503). It does not retry, rotate identities, or use solver services. For pages that matter and were skipped, a person opens them in a normal browser session and saves them by hand. This is ordinary browsing at human speed.

## Steps

1. Run the crawler, then open `data/raw/crawl_report.json` and look at `not_ok`.
2. Open each needed URL in Chrome. Wait until the page has fully loaded.
3. Save it with **File → Save Page As… → Format: Webpage, HTML Only**.
4. Save into `data/manual/<site>/`, where `<site>` is `wasalt` or `darglobal`.
   Name the file `<slug>_<YYYY-MM-DD>.html`, e.g. `apartments-for-sale-in-riyadh_2026-10-31.html`.
   The date becomes the snapshot date.
5. Run `uv run python -m crawler.extract`.

The extractor finds the page URL from Chrome's `<!-- saved from url=... -->` comment, or from the page's canonical link. Files with neither are skipped and reported.

`data/` is git-ignored. Captured pages are never committed to the public repo.
