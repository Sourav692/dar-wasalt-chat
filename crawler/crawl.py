"""Polite, robots-aware crawler.

Renders pages in a real Chromium so client-side content appears. It never tries to get
past a bot challenge: a challenged page is skipped and recorded, and is a candidate for
manual capture (see crawler/manual/README.md).

Usage: uv run python -m crawler.crawl [--only darglobal|wasalt]
Output: data/raw/<site>/<sha1>.html, data/raw/manifest.jsonl, data/raw/crawl_report.json
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from playwright.async_api import BrowserContext, Page, async_playwright
from protego import Protego

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SEEDS = Path(__file__).with_name("seeds.yaml")

ROBOT_NAME = "DarWasaltResearchBot"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/156.0 Safari/537.36 {ROBOT_NAME}/0.1 (+https://github.com/Sourav692/dar-wasalt-chat)"
)

CHALLENGE_MARKERS = (
    "performing security verification",
    "just a moment",
    "verify you are human",
    "incapsula incident",
    "request unsuccessful",
    "attention required",
)


def is_challenge(status: int | None, title: str, body_text: str) -> bool:
    """True when the response is a bot-protection page rather than real content."""
    if status in (403, 429, 503):
        return True
    head = f"{title}\n{body_text[:2000]}".lower()
    return any(m in head for m in CHALLENGE_MARKERS)


def in_scope(url: str, hosts: list[str]) -> bool:
    """https on an allowlisted host only. Blocks off-site sitemap entries and redirects."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in hosts and parts.port in (None, 443)


def apply_scope(
    urls: list[str], hosts: list[str], exclude: list[str], caps: dict[str, int]
) -> list[str]:
    """Keep allowlisted hosts, dedupe (order kept), drop excluded paths, cap prefixes."""
    seen: set[str] = set()
    used: Counter[str] = Counter()
    out = []
    for url in urls:
        url = url.rstrip("/") or url
        path = urlsplit(url).path
        if url in seen or not in_scope(url, hosts) or any(x in path for x in exclude):
            continue
        prefix = next((p for p in caps if path.startswith(p)), None)
        if prefix:
            if used[prefix] >= caps[prefix]:
                continue
            used[prefix] += 1
        seen.add(url)
        out.append(url)
    return out


def raw_path(site: str, url: str) -> Path:
    return RAW / site / f"{hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()}.html"


class HostGate:
    """robots.txt rules plus a minimum delay, per host."""

    def __init__(self, delay: float):
        self.delay = delay
        self.rules: dict[str, Protego | None] = {}
        self.last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        gap = self.delay - (time.monotonic() - self.last.get(host, 0))
        if gap > 0:
            await asyncio.sleep(gap)
        self.last[host] = time.monotonic()

    async def allowed(self, ctx: BrowserContext, page: Page, url: str) -> bool:
        host = urlsplit(url).netloc
        if host not in self.rules:
            status, text = await fetch_text(ctx, page, self, f"https://{host}/robots.txt")
            if status == 404:
                self.rules[host] = Protego.parse("")  # no robots.txt = everything allowed
            elif status == 200:
                self.rules[host] = Protego.parse(text)
            else:
                # Unreadable robots.txt: be conservative and fetch nothing from this host.
                print(f"[robots] {host}: unreadable (status={status}); host skipped")
                self.rules[host] = None
        rules = self.rules[host]
        return rules is not None and rules.can_fetch(url, ROBOT_NAME)


async def fetch_text(
    ctx: BrowserContext, page: Page, gate: HostGate, url: str
) -> tuple[int | None, str]:
    """Fetch robots.txt or a sitemap. Plain request first, rendered page if that is challenged."""
    host = urlsplit(url).netloc
    await gate.wait(host)
    r = await ctx.request.get(url, timeout=30000)
    body = await r.body()
    text = (gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body).decode(errors="ignore")
    if not is_challenge(r.status, "", text):
        return r.status, text
    await gate.wait(host)
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2000)
    html = await page.content()
    status = resp.status if resp else None
    if is_challenge(status, await page.title(), await page.inner_text("body")):
        return 403, ""
    return status, html


async def crawl_site(ctx: BrowserContext, gate: HostGate, site: str, cfg: dict, wait_ms: int):
    page = await ctx.new_page()
    hosts = cfg["hosts"]
    urls = list(cfg.get("urls", []))
    for sm in cfg.get("sitemaps", []):
        if not in_scope(sm, hosts):
            print(f"[{site}] sitemap off-scope, skipped: {sm}")
            continue
        if not await gate.allowed(ctx, page, sm):
            print(f"[{site}] sitemap disallowed by robots: {sm}")
            continue
        status, text = await fetch_text(ctx, page, gate, sm)
        found = re.findall(r"<loc>\s*(.*?)\s*</loc>", text)
        print(f"[{site}] sitemap {sm}: status={status}, {len(found)} urls")
        urls += found
    urls = apply_scope(urls, hosts, cfg.get("exclude", []), cfg.get("caps", {}))
    print(f"[{site}] {len(urls)} urls in scope")

    done = load_done()
    (RAW / site).mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(urls, 1):
        if url in done:
            continue
        rec = {
            "site": site,
            "url": url,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not await gate.allowed(ctx, page, url):
            rec["outcome"] = "skipped_robots"
        else:
            rec.update(await fetch_page(page, gate, site, url, wait_ms, hosts))
        append_manifest(rec)
        print(f"[{site}] {i}/{len(urls)} {rec['outcome']:<16} {url}")
    await page.close()


async def fetch_page(
    page: Page, gate: HostGate, site: str, url: str, wait_ms: int, hosts: list[str]
) -> dict:
    for attempt in range(3):  # retries cover network errors only, never challenges
        await gate.wait(urlsplit(url).netloc)
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if not in_scope(page.url, hosts):  # redirected off-site: discard
                return {"outcome": "skipped_offscope", "final_url": page.url}
            await page.wait_for_timeout(wait_ms)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            status = resp.status if resp else None
            title = await page.title()
            if is_challenge(status, title, await page.inner_text("body")):
                return {"outcome": "skipped_challenge", "status": status}
            path = raw_path(site, url)
            path.write_text(await page.content(), encoding="utf-8")
            return {
                "outcome": "ok",
                "status": status,
                "final_url": page.url,
                "title": title,
                "path": str(path.relative_to(ROOT)),
            }
        except Exception as e:  # noqa: BLE001 - recorded in the manifest, crawl continues
            err = f"{type(e).__name__}: {e}"[:200]
            await asyncio.sleep(5 * (attempt + 1))
    return {"outcome": "error", "error": err}


def load_done() -> set[str]:
    """URLs already fetched successfully, so re-runs don't hit the sites again."""
    m = RAW / "manifest.jsonl"
    if not m.exists():
        return set()
    recs = [json.loads(line) for line in m.read_text().splitlines() if line]
    return {r["url"] for r in recs if r["outcome"] == "ok"}


def append_manifest(rec: dict) -> None:
    with (RAW / "manifest.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_report() -> None:
    recs = [json.loads(line) for line in (RAW / "manifest.jsonl").read_text().splitlines() if line]
    latest = {r["url"]: r for r in recs}  # last attempt per URL wins
    report = {
        site: dict(Counter(r["outcome"] for r in latest.values() if r["site"] == site))
        for site in sorted({r["site"] for r in latest.values()})
    }
    report["not_ok"] = [
        {"url": u, "outcome": r["outcome"]} for u, r in latest.items() if r["outcome"] != "ok"
    ]
    (RAW / "crawl_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "not_ok"}, indent=2))


async def main(only: str | None) -> None:
    cfg = yaml.safe_load(SEEDS.read_text())
    RAW.mkdir(parents=True, exist_ok=True)
    gate = HostGate(cfg["delay_seconds"])
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-US")
        sites = {k: v for k, v in cfg["sites"].items() if only in (None, k)}
        # Sites run in parallel; requests within a host stay sequential and spaced.
        await asyncio.gather(
            *(crawl_site(ctx, gate, s, c, cfg["render_wait_ms"]) for s, c in sites.items())
        )
        await browser.close()
    write_report()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["darglobal", "wasalt"])
    asyncio.run(main(ap.parse_args().only))
