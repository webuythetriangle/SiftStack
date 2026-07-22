"""Scrapfly-based alternative to captcha_solver.py / ncnotices_scraper.py's Turnstile solve.

NOT WIRED IN. Nothing imports this module yet — it exists so the Scrapfly path
can be swapped in later with a one-line import change (see "Wiring this in"
below), without touching scraper.py / ncnotices_scraper.py call sites.

Why this looks different from captcha_solver.py:
2Captcha solves *just the CAPTCHA token* and hands it back to the still-open
Playwright page — captcha_solver.py injects that token into the page and
clicks "View Notice". Scrapfly works a layer up: you hand it a URL and it
does the *entire fetch* itself (residential proxy + real/emulated browser
fingerprint + JS execution), solving whatever anti-bot challenge is present
— reCAPTCHA, Cloudflare Turnstile, or an outright IP block — as part of that
fetch. There's no token to inject back into Playwright, so instead this loads
Scrapfly's already-unlocked HTML into the *same* Playwright page via
page.set_content(), keeping page.url unchanged so every downstream caller
(notice_parser.parse_notice_page, its embedded-PDF fallback, etc.) keeps
working with zero changes.

This also covers the one gap 2Captcha can't: captcha_solver.py bails
immediately when tnpublicnotice.com says "You are not permitted to view
public notices" (an IP block, not a CAPTCHA) — Scrapfly's residential proxy
pool routes around that instead of just failing.

Wiring this in (when/if you want it):
  scraper.py:
    from captcha_solver import solve_captcha_and_view
    -->
    from scrapfly_solver import solve_captcha_and_view

  ncnotices_scraper.py (its solver is a private module-level function, not
  imported from captcha_solver.py, so replace the call site directly):
    if not await _solve_turnstile_and_view(page):
    -->
    if not await solve_captcha_and_view(page):
    # and add: from scrapfly_solver import solve_captcha_and_view

Setup required before use — see the walkthrough in the PR/chat that added
this file: create a Scrapfly account, grab an API key, set SCRAPFLY_API_KEY
in .env (and optionally SCRAPFLY_PROXY_POOL — defaults to
"public_residential_pool"), pip install -r requirements.txt.
"""

import logging

from playwright.async_api import Page
from scrapfly import ScrapeConfig, ScrapflyClient, ScrapflyScrapeError

import config

logger = logging.getLogger(__name__)

_client: ScrapflyClient | None = None


def _get_client() -> ScrapflyClient:
    global _client
    if _client is None:
        _client = ScrapflyClient(key=config.SCRAPFLY_API_KEY)
    return _client


async def _fetch_unlocked_html(page: Page) -> str | None:
    """Re-fetch the current page URL through Scrapfly, carrying over the
    logged-in session's cookies, and return the unlocked HTML (or None)."""
    if not config.SCRAPFLY_API_KEY:
        logger.error("SCRAPFLY_API_KEY not set — cannot use Scrapfly fallback")
        return None

    # Carry the Playwright session's login/ViewState cookies over to Scrapfly's
    # fetch — these notice pages are session-gated ASP.NET pages, not public.
    cookies = await page.context.cookies()
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    scrape_config = ScrapeConfig(
        url=page.url,
        asp=True,
        render_js=True,
        proxy_pool=config.SCRAPFLY_PROXY_POOL,
        country="us",
        headers={"Cookie": cookie_header} if cookie_header else None,
        tags=config.SCRAPFLY_TAGS,
    )

    try:
        result = await _get_client().async_scrape(scrape_config)
    except ScrapflyScrapeError:
        logger.exception("Scrapfly scrape failed for %s", page.url)
        return None

    return result.content


async def solve_captcha_and_view(page: Page) -> bool:
    """Drop-in replacement for captcha_solver.solve_captcha_and_view (and,
    via the alias below, ncnotices_scraper._solve_turnstile_and_view).

    Same contract as both originals: returns True once the notice text is
    visible in `page`'s DOM (so notice_parser.parse_notice_page can read it
    via page.inner_text("body") exactly as it does today).
    """
    content_el = await page.query_selector("text='Notice Content'")
    if content_el:
        logger.info("Notice content already visible — no fetch needed")
        return True

    logger.warning("Fetching %s via Scrapfly (anti-bot + proxy)", page.url)
    html = await _fetch_unlocked_html(page)
    if not html:
        return False

    await page.set_content(html, wait_until="domcontentloaded")

    content_el = await page.query_selector("text='Notice Content'")
    if content_el:
        logger.warning("Scrapfly unlocked notice text for %s", page.url)
        return True

    logger.warning(
        "Scrapfly fetch succeeded but notice content still not visible: %s", page.url
    )
    return False


# ncnotices_scraper.py's Turnstile call site can point at this same function —
# Scrapfly's ASP handles reCAPTCHA v2 and Cloudflare Turnstile identically
# (asp=True is the one parameter for both), so no separate implementation
# is needed the way captcha_solver.py / ncnotices_scraper.py require.
solve_turnstile_and_view = solve_captcha_and_view
