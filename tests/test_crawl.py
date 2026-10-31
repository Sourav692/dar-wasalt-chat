from protego import Protego

from crawler.crawl import ROBOT_NAME, apply_scope, is_challenge

# Excerpt of darglobal.co.uk/robots.txt (2026-10-31): wildcard rules stdlib robotparser ignores.
DAR_ROBOTS = """
User-agent: *
Disallow: /sitemap_index.xml
Disallow: /*utm_source=
Disallow: /*?s=
Allow: /$
Allow: /projects/
"""


def test_cloudflare_challenge_detected():
    body = "wasalt.sa\nPerforming security verification\nThis website uses a security service"
    assert is_challenge(200, "Just a moment...", body)


def test_imperva_block_detected():
    assert is_challenge(200, "", "Request unsuccessful. Incapsula incident ID: 123")


def test_blocking_status_codes_are_challenges():
    for status in (403, 429, 503):
        assert is_challenge(status, "Some page", "normal text")


def test_real_page_is_not_challenge():
    title = "DarGlobal | International Luxury Real Estate Developer"
    assert not is_challenge(200, title, "LONDON-LISTED LUXURY REAL ESTATE DEVELOPER")


def test_robots_wildcards_respected():
    rp = Protego.parse(DAR_ROBOTS)
    assert rp.can_fetch("https://darglobal.co.uk/projects/x", ROBOT_NAME)
    assert rp.can_fetch("https://darglobal.co.uk/sitemap.xml", ROBOT_NAME)
    assert not rp.can_fetch("https://darglobal.co.uk/sitemap_index.xml", ROBOT_NAME)
    assert not rp.can_fetch("https://darglobal.co.uk/about?utm_source=x", ROBOT_NAME)
    assert not rp.can_fetch("https://darglobal.co.uk/?s=villa", ROBOT_NAME)


def test_scope_dedupes_excludes_and_caps():
    urls = [
        "https://x.com/about",
        "https://x.com/about/",
        "https://x.com/pay-online/status",
        "https://x.com/blog/a",
        "https://x.com/blog/b",
        "https://x.com/blog/c",
    ]
    out = apply_scope(urls, exclude=["/pay-online"], caps={"/blog/": 2})
    assert out == ["https://x.com/about", "https://x.com/blog/a", "https://x.com/blog/b"]
