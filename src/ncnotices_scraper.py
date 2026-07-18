"""Scraper for ncnotices.com (North Carolina Press Association public notices).

Second scrape source alongside tnpublicnotice.com (see scraper.py), added for
NC market expansion. Same underlying WebStrides ASP.NET platform, but:
  - No login required — basic search is open.
  - Search is keyword-based (NCSearch.keyword) against a free-text index,
    not a named "saved search" dropdown.
  - County/date-range filters live in collapsed panels that must be opened
    (click the toggle div) before their inputs become interactable.
  - The date filter is a radio-button "In the last N days/weeks/months" or a
    custom From/To range — we use "In the last N days" for both daily and
    historical modes (radio is checked by default; historical just uses a
    larger N).
  - Each notice detail page (Details.aspx) is gated by a Cloudflare Turnstile
    challenge (not Google reCAPTCHA) — see NC_TURNSTILE_SITEKEY in config.py.

Scope: foreclosure notices only. See config.NC_SAVED_SEARCHES for why the
other 5 notice types aren't covered by this source.
"""

import asyncio
import logging
import random
import re
from datetime import datetime

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright
from twocaptcha import TwoCaptcha

import config
from config import (
    NC_SEARCH_URL,
    NC_SEL_COUNTY_LIST,
    NC_SEL_COUNTY_TOGGLE,
    NC_SEL_DATE_TOGGLE,
    NC_SEL_LAST_NUM_DAYS_INPUT,
    NC_SEL_NEXT_PAGE_BUTTON,
    NC_SEL_PAGE_INFO,
    NC_SEL_PER_PAGE_DROPDOWN,
    NC_SEL_SEARCH_KEYWORD,
    NC_SEL_SEARCH_SUBMIT,
    NC_SEL_TURNSTILE_RESPONSE,
    NC_SEL_VIEW_BUTTON_PATTERN,
    NC_SEL_VIEW_NOTICE_BUTTON,
    NC_TURNSTILE_SITEKEY,
    MAX_RETRIES,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    RESULTS_PER_PAGE,
    NCSearch,
)
from data_formatter import _notice_id_from_url
from foreclosure_filter import is_valid_foreclosure
from nc_notice_parser import parse_nc_notice_text
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


async def delay() -> None:
    wait = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(wait)


# ── Search form setup ────────────────────────────────────────────────


async def _open_panel(page: Page, toggle_selector: str) -> None:
    """Click a collapsed filter-panel toggle (county/date) and wait for it to render."""
    await page.click(toggle_selector)
    await page.wait_for_timeout(300)


async def _select_county(page: Page, county: str) -> bool:
    """Open the county panel and check the checkbox matching `county` by label text.

    Returns True on success. Uses label text lookup rather than a hardcoded
    checkbox index — the option list is alphabetical and index-stable in
    practice, but matching by label is robust to the site adding/removing
    counties.
    """
    await _open_panel(page, NC_SEL_COUNTY_TOGGLE)
    checkbox_id = await page.evaluate(
        """(county) => {
            const labels = document.querySelectorAll('label[for^="ctl00_ContentPlaceHolder1_as1_lstCounty_"]');
            for (const label of labels) {
                if (label.innerText.trim().toLowerCase() === county.toLowerCase()) {
                    return label.getAttribute('for');
                }
            }
            return null;
        }""",
        county,
    )
    if not checkbox_id:
        logger.error("Could not find county checkbox for '%s'", county)
        return False

    await page.evaluate(f'document.getElementById("{checkbox_id}").click()')
    await page.wait_for_timeout(1200)
    await page.wait_for_load_state("networkidle")

    checked = await page.eval_on_selector(f"#{checkbox_id}", "el => el.checked")
    if not checked:
        logger.error("County checkbox for '%s' did not register as checked", county)
        return False
    return True


async def _set_date_range_days(page: Page, days: int) -> None:
    """Open the date panel and set 'In the last N days' (radio is checked by default)."""
    await _open_panel(page, NC_SEL_DATE_TOGGLE)
    await page.fill(NC_SEL_LAST_NUM_DAYS_INPUT, str(days))


async def _get_page_info(page: Page) -> tuple[int, int]:
    """Parse 'Page X of Y Pages' text. Returns (current_page, total_pages)."""
    try:
        info_el = await page.query_selector(NC_SEL_PAGE_INFO)
        if info_el:
            text = await info_el.inner_text()
            m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text)
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1, 1


async def _set_per_page(page: Page) -> None:
    dropdown = await page.query_selector(NC_SEL_PER_PAGE_DROPDOWN)
    if dropdown:
        current = await dropdown.input_value()
        if current != str(RESULTS_PER_PAGE):
            try:
                await page.select_option(NC_SEL_PER_PAGE_DROPDOWN, str(RESULTS_PER_PAGE))
                await page.wait_for_load_state("networkidle")
                await delay()
            except Exception:
                logger.debug("Could not set per-page to %d (option may not exist)", RESULTS_PER_PAGE)


async def run_nc_search(
    page: Page,
    search: NCSearch,
    days_back: int,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Run one NCSearch (county + keyword), paginate, and scrape each notice."""
    logger.info("Running NC search: %s / %s (last %d days)", search.county, search.keyword, days_back)

    await page.goto(NC_SEARCH_URL, wait_until="networkidle", timeout=30_000)
    await delay()

    await page.fill(NC_SEL_SEARCH_KEYWORD, search.keyword)
    await _set_date_range_days(page, days_back)

    if not await _select_county(page, search.county):
        return []

    await page.click(NC_SEL_SEARCH_SUBMIT, force=True)
    await page.wait_for_timeout(1500)
    await page.wait_for_load_state("networkidle")

    body_text = await page.inner_text("body")
    if "No public notices found" in body_text:
        logger.info("  0 results for %s / %s", search.county, search.keyword)
        return []

    await _set_per_page(page)

    notices: list[NoticeData] = []
    current_page, total_pages = await _get_page_info(page)
    logger.info("  %d page(s) of results", total_pages)

    while True:
        logger.info("  Scraping page %d/%d", current_page, total_pages)
        remaining = (max_notices - len(notices)) if max_notices else 0
        page_notices, page_crashed = await _scrape_nc_results_page(
            page, search, seen_ids, max_notices=remaining,
        )
        notices.extend(page_notices)

        if max_notices and len(notices) >= max_notices:
            notices = notices[:max_notices]
            break

        if page_crashed:
            logger.error("  Browser page crashed — stopping this search early")
            break

        if current_page >= total_pages:
            break

        next_btn = await page.query_selector(NC_SEL_NEXT_PAGE_BUTTON)
        can_advance = next_btn and not await next_btn.get_attribute("disabled") if next_btn else False
        if not can_advance:
            break

        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await delay()
        current_page, total_pages = await _get_page_info(page)

    logger.info("  Found %d notices for %s / %s", len(notices), search.county, search.keyword)
    return notices


async def _scrape_nc_results_page(
    page: Page,
    search: NCSearch,
    seen_ids: dict[str, str] | None = None,
    max_notices: int = 0,
) -> tuple[list[NoticeData], bool]:
    """Click each view button, solve Turnstile, parse the notice.

    Returns (notices, page_crashed). max_notices=0 means no cap.
    """
    notices: list[NoticeData] = []

    try:
        await page.wait_for_selector(NC_SEL_VIEW_BUTTON_PATTERN, state="attached", timeout=20_000)
    except PwTimeout:
        logger.warning("  No view buttons found on this page")
        return notices, False

    view_buttons = await page.query_selector_all(NC_SEL_VIEW_BUTTON_PATTERN)
    num_results = len(view_buttons)
    logger.info("  %d results on this page", num_results)

    for idx in range(num_results):
        if max_notices and len(notices) >= max_notices:
            break
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                view_buttons = await page.query_selector_all(NC_SEL_VIEW_BUTTON_PATTERN)
                if idx >= len(view_buttons):
                    break
                btn = view_buttons[idx]

                # Row metadata: publication/date/city/county, captured before navigating.
                row = await btn.evaluate_handle(
                    "el => el.closest('table.nested') || el.closest('tr')"
                )
                row_text = ""
                try:
                    row_text = await row.evaluate("el => el.innerText")
                except Exception:
                    pass
                city_hint, county_hint = _extract_row_city_county(row_text)

                # View button navigates directly (location.href) — no postback needed.
                await btn.click()
                await page.wait_for_load_state("load", timeout=20_000)
                await page.wait_for_timeout(1000)

                notice_id = _notice_id_from_url(page.url)
                if seen_ids is not None and notice_id and notice_id in seen_ids:
                    logger.info("  Skipping already-processed notice ID=%s", notice_id)
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                    await delay()
                    break

                if not await _solve_turnstile_and_view(page):
                    logger.warning("  Turnstile solve failed for result %d (attempt %d)", idx + 1, attempt)
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                    await delay()
                    continue

                full_text = await page.inner_text("body")
                notice = parse_nc_notice_text(
                    full_text,
                    county=county_hint or search.county,
                    notice_type=search.notice_type,
                    source_url=page.url,
                    city_hint=city_hint,
                )

                if seen_ids is not None and notice_id:
                    seen_ids[notice_id] = notice.date_added or datetime.now().strftime("%Y-%m-%d")

                if not is_valid_foreclosure(notice):
                    logger.debug("  Filtered out (not foreclosure): %s", notice.source_url)
                else:
                    notices.append(notice)
                    logger.debug("  Kept notice: %s", notice.source_url)

                await page.go_back()
                await page.wait_for_load_state("networkidle")
                if "details" in page.url.lower():
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                await delay()
                break

            except PwTimeout:
                logger.warning("  Timeout on result %d (attempt %d/%d)", idx + 1, attempt, MAX_RETRIES)
                try:
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                except Exception:
                    pass
                await delay()
            except Exception as exc:
                logger.exception("  Error on result %d (attempt %d/%d)", idx + 1, attempt, MAX_RETRIES)
                if "page crashed" in str(exc).lower() or page.is_closed():
                    logger.error("  Browser page is dead — abandoning rest of this page's results")
                    return notices, True
                if "search" not in page.url.lower():
                    try:
                        await page.go_back()
                        await page.wait_for_load_state("networkidle")
                    except Exception:
                        pass
                await delay()

    return notices, False


def _extract_row_city_county(row_text: str) -> tuple[str, str]:
    """Parse 'City: X' / 'County: Y' out of a result row's hidden metadata div."""
    city, county = "", ""
    m = re.search(r"City:\s*([\w\s.\-]+?)(?:\n|$)", row_text)
    if m:
        city = m.group(1).strip()
    m = re.search(r"County:\s*([\w\s.\-]+?)(?:\n|$)", row_text)
    if m:
        county = m.group(1).strip()
    return city, county


# ── Turnstile solving ────────────────────────────────────────────────


async def _solve_turnstile_and_view(page: Page) -> bool:
    """Solve Cloudflare Turnstile via 2Captcha and click 'I Agree, View Notice'.

    Mirrors captcha_solver.solve_captcha_and_view's structure but for
    Turnstile (ncnotices.com) instead of Google reCAPTCHA v2 (tnpublicnotice.com).
    """
    if not config.CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set — cannot solve Turnstile challenge")
        return False

    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Already visible (Turnstile previously solved this session)?
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                return True

            view_btn = await page.query_selector(NC_SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                logger.warning(
                    "  'I Agree, View Notice' button not found on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            logger.warning("  Solving Turnstile for %s (attempt %d/%d)", page_url, attempt, MAX_RETRIES)
            solver = TwoCaptcha(config.CAPTCHA_API_KEY)
            result = await asyncio.to_thread(
                solver.turnstile, sitekey=NC_TURNSTILE_SITEKEY, url=page_url,
            )
            token = result.get("code") if isinstance(result, dict) else str(result)
            if not token:
                logger.warning("  2Captcha returned empty Turnstile token (attempt %d)", attempt)
                continue

            await page.evaluate(
                """(token) => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    if (el) { el.value = token; }
                }""",
                token,
            )

            view_btn = await page.query_selector(NC_SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                content_el = await page.query_selector("text='Notice Content'")
                if content_el:
                    return True
                logger.warning("  View Notice button gone after token inject (attempt %d)", attempt)
                continue

            await view_btn.click()
            await page.wait_for_load_state("networkidle")

            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.warning("  Turnstile solved — notice text visible")
                return True

            challenge_msg = await page.query_selector("text='You must complete the challenge'")
            if not challenge_msg:
                logger.warning("  Turnstile solved — gate cleared")
                return True

            logger.warning("  Turnstile still present after attempt %d", attempt)

        except Exception:
            logger.exception("  Turnstile solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("  All %d Turnstile attempts failed for %s", MAX_RETRIES, page_url)
    return False


# ── Main entry point ─────────────────────────────────────────────────


async def scrape_all_nc(
    mode: str = "daily",
    searches: list[NCSearch] | None = None,
    days_back: int | None = None,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Scrape ncnotices.com for the given NCSearch list.

    Args:
        mode: "daily" (default 7-day window) or "historical" (default 365 days).
              Ignored if days_back is explicitly set.
        days_back: Explicit lookback window in days — overrides mode default.
    """
    if searches is None:
        searches = config.NC_SAVED_SEARCHES
    if seen_ids is None:
        seen_ids = {}

    if days_back is None:
        days_back = 365 if mode == "historical" else 7

    all_notices: list[NoticeData] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        context.set_default_timeout(60_000)
        page = await context.new_page()

        for search in searches:
            remaining = (max_notices - len(all_notices)) if max_notices else 0
            try:
                search_notices = await run_nc_search(
                    page, search, days_back, max_notices=remaining, seen_ids=seen_ids,
                )
                all_notices.extend(search_notices)
            except Exception:
                logger.exception("Failed to scrape NC search: %s / %s", search.county, search.keyword)

            if max_notices and len(all_notices) >= max_notices:
                break

        await browser.close()

    logger.info("Total NC notices scraped: %d", len(all_notices))
    return all_notices
