"""Scraper for the NC eCourts Portal (Tyler Technologies Odyssey — statewide
public case search, all 100 NC counties as of the Oct 13, 2025 rollout).

Third scrape source alongside tnpublicnotice.com (scraper.py) and
ncnotices.com (ncnotices_scraper.py), added to catch NC foreclosures earlier
in the process. A power-of-sale foreclosure is filed as a Special Proceeding
(NCGS Chapter 45, Article 2A) when the trustee/substitute trustee files a
"Notice of Hearing" with the Clerk of Superior Court — this happens BEFORE
any sale notice is published in the newspaper (ncnotices_scraper.py's
source, which only sees the case weeks later at publication, after the
hearing has already found the trustee has the right to foreclose). Catching
the Special Proceeding filing here is materially first-to-market.

## Why Scrapfly instead of 2Captcha here

portal-nc.tylertech.cloud is protected by AWS WAF Bot Control with a CAPTCHA
"Human Verification" interstitial (confirmed live 2026-07-19 — see
config.ECOURTS_BASE_URL's comment) — not Google reCAPTCHA
(tnpublicnotice.com) or Cloudflare Turnstile (ncnotices.com). 2Captcha has no
turnkey AWS WAF token-solving flow the way it does for reCAPTCHA v2/Turnstile,
so this source is built entirely on Scrapfly's ASP (anti-scraping-protection)
bypass — no local Playwright browser is used at all. Every page load in this
module is a Scrapfly `async_scrape` call; `session=` keeps cookies/fingerprint
consistent across the multi-step search → results → case-detail flow, the
same way a normal scraper reuses one Playwright page.

## Confirmed dead end (2026-07-20) — Scrapfly's Web API cannot reach this target

During initial build-out, Scrapfly's ASP did NOT clear this specific WAF on
any of 3 live attempts (`ERR::ASP::SHIELD_PROTECTION_FAILED` — "Unable to
bypass portal-nc.tylertech.cloud"). This was filed as a support ticket with
Scrapfly; their reply (2026-07-20) confirms it's not a tuning/quota problem:

  "This domain currently requires human verification, even when accessed
  through a real browser, which is not supported by the Web API. As an
  alternative, you can use our Cloud Browser with Human-in-the-Loop (HITL)
  support."

In other words `async_scrape`/`ScrapeConfig` (what `_scrapfly_fetch()` below
uses) is architecturally incapable of passing this WAF's human-verification
challenge, no matter how `asp`/`render_js`/proxy_pool are tuned — retrying it
here is retrying a call that cannot succeed.

Re-confirmed 2026-07-20 after fixing three unrelated `js_scenario` config
bugs that had been silently 400ing every request before it ever reached the
target (wait_for_selector timeout >15000ms, `click.xpath` instead of a CSS
`click.selector`, wait_for_navigation timeout >10000ms — all fixed below).
With those fixed, the request finally reaches the target, and hits exactly
`ERR::ASP::SHIELD_PROTECTION_FAILED` on all 5 counties, all 3 retries each.
So this isn't a config problem masking as a WAF problem — it's genuinely the
WAF, full stop. Scrapfly's suggested fix, Cloud
Browser + HITL, is a different product: a persistent remote browser session
where a human manually clicks through the verification challenge when it
appears, which does not fit this module's current unattended-cron model
(`ecourts-daily`/`ecourts-historical` are meant to run with no one watching)
without adding a manual-intervention step. This has NOT been built.

Options going forward (none implemented yet — pick one before resuming work
on this source):
  - Build the Cloud Browser + HITL flow and accept a human must be available
    to clear the challenge each run (or each session-cookie expiry) —
    defeats unattended daily scheduling as currently designed.
  - Evaluate 2Captcha's "Amazon WAF" task type (separate from the
    reCAPTCHA/Turnstile helpers already used elsewhere in this project) as a
    fully-automated alternative — untested against this specific target.
  - Drop the eCourts source and rely on ncnotices.com's later-published
    Notice of Sale data only, accepting the lost first-to-market window
    (see module docstring above — eCourts exists specifically to catch the
    earlier Special Proceeding filing).

## Search form field names — not yet live-verified

The Smart Search "Advanced Filtering Options" are documented publicly as
"Civil Actions, Special Proceedings (non-confidential), Estates, and
Criminal Actions" (nccourts.gov Portal FAQ) — that confirms the exact
"Special Proceedings" filter label used below. The rest of the form
(county selector, date range, submit button) is targeted by visible label
text using the same resilience pattern as ncnotices_scraper.py's
`_select_county` (label-text lookup, not a hardcoded element ID), since the
underlying DOM couldn't be inspected directly (every raw Playwright
navigation attempt hit the WAF challenge before Scrapfly was even involved).
Run `python src/ecourts_scraper.py --inspect --county Wake` to dump the
Scrapfly-fetched search page HTML to `output/ecourts_inspect_*.html` for
calibrating these selectors against the live DOM once ASP bypass is working.
"""

import argparse
import asyncio
import logging
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from scrapfly import ScrapeApiResponse, ScrapeConfig, ScrapflyClient, ScrapflyAspError

import config
from ecourts_notice_parser import (
    is_foreclosure_special_proceeding,
    is_target_ecourts_county,
    parse_ecourts_case_text,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

_client: ScrapflyClient | None = None


def _get_client() -> ScrapflyClient:
    global _client
    if _client is None:
        _client = ScrapflyClient(key=config.SCRAPFLY_API_KEY)
    return _client


# ── Scrapfly fetch helper ────────────────────────────────────────────────

ASP_SHIELD_RETRIES = 3
ASP_SHIELD_RETRY_DELAY = 5.0  # seconds — Scrapfly's own error message suggests "retry in few seconds"


async def _scrapfly_fetch(
    url: str,
    session: str,
    js_scenario: list[dict] | None = None,
) -> str | None:
    """Fetch a URL through Scrapfly's ASP bypass, retrying shield failures.

    `session` pins the request to a persistent Scrapfly-managed browser
    fingerprint/cookie jar so a multi-step flow (load form -> submit search
    -> open case detail) looks like one continuous visit, not three unrelated
    ones — the same reason scraper.py/ncnotices_scraper.py reuse one
    Playwright `page` throughout a run.
    """
    if not config.SCRAPFLY_API_KEY:
        logger.error("SCRAPFLY_API_KEY not set — cannot fetch eCourts Portal")
        return None

    cfg_kwargs = dict(
        url=url,
        asp=True,
        render_js=True,
        proxy_pool=config.SCRAPFLY_PROXY_POOL,
        country="us",
        session=session,
        tags=config.SCRAPFLY_TAGS,
    )
    if js_scenario:
        cfg_kwargs["js_scenario"] = js_scenario

    for attempt in range(1, ASP_SHIELD_RETRIES + 1):
        try:
            result: ScrapeApiResponse = await _get_client().async_scrape(ScrapeConfig(**cfg_kwargs))
            return result.content
        except ScrapflyAspError:
            logger.warning(
                "  Scrapfly ASP shield failed for %s (attempt %d/%d)",
                url, attempt, ASP_SHIELD_RETRIES,
            )
            if attempt < ASP_SHIELD_RETRIES:
                await asyncio.sleep(ASP_SHIELD_RETRY_DELAY)
        except Exception:
            logger.exception("  Scrapfly fetch error for %s (attempt %d/%d)", url, attempt, ASP_SHIELD_RETRIES)
            if attempt < ASP_SHIELD_RETRIES:
                await asyncio.sleep(ASP_SHIELD_RETRY_DELAY)

    logger.error("  All %d Scrapfly attempts failed for %s", ASP_SHIELD_RETRIES, url)
    return None


# ── Search form scenario ─────────────────────────────────────────────────


def _build_search_scenario(county: str, days_back: int) -> list[dict]:
    """Scrapfly js_scenario steps to fill and submit Smart Search.

    Finds fields by visible label/placeholder text (not hardcoded IDs),
    mirroring ncnotices_scraper.py._select_county's robustness strategy,
    since the live DOM wasn't directly inspectable (WAF blocked raw
    Playwright navigation during development — see module docstring).
    "Special Proceedings" is confirmed as the exact case-category filter
    label (nccourts.gov Portal FAQ); the rest are best-effort label matches
    pending live calibration via --inspect.
    """
    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%m/%d/%Y")

    fill_js = f"""
        (() => {{
            const byLabelText = (text) => {{
                const labels = Array.from(document.querySelectorAll('label'));
                const label = labels.find(l => l.innerText.trim().toLowerCase().includes(text.toLowerCase()));
                if (!label) return null;
                if (label.htmlFor) return document.getElementById(label.htmlFor);
                return label.querySelector('input, select');
            }};
            const clickByText = (tag, text) => {{
                const els = Array.from(document.querySelectorAll(tag));
                const el = els.find(e => e.innerText && e.innerText.trim().toLowerCase().includes(text.toLowerCase()));
                if (el) {{ el.click(); return true; }}
                return false;
            }};
            const setNative = (el, value) => {{
                const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }};

            // Case category: check "Special Proceedings"
            clickByText('label', 'special proceedings') || clickByText('span', 'special proceedings');

            // County selector
            const countyField = byLabelText('county');
            if (countyField) setNative(countyField, {county!r});

            // Date range ("Filed From" / "Date Filed")
            const dateField = byLabelText('filed') || byLabelText('date from');
            if (dateField) setNative(dateField, {since_date!r});

            return true;
        }})()
    """

    # Scrapfly's built-in {{"click": ...}} scenario step only accepts a CSS
    # `selector` (confirmed live 2026-07-20 — "Stage click.selector is
    # required" when an `xpath` key was used instead), and there's no
    # text-matching in plain CSS to find a button by its label. So the
    # search-button click is done the same way as the field fills above: an
    # `execute` step running our own JS (clickByText), not Scrapfly's `click`
    # step.
    click_search_js = """
        (() => {
            const clickByText = (tag, text) => {
                const els = Array.from(document.querySelectorAll(tag));
                const el = els.find(e => e.innerText && e.innerText.trim().toLowerCase().includes(text.toLowerCase()));
                if (el) { el.click(); return true; }
                return false;
            };
            return clickByText('button', 'search') || clickByText('button', 'find')
                || clickByText('a', 'search') || clickByText('a', 'find');
        })()
    """

    return [
        # Scrapfly's per-stage timeout caps differ by stage (confirmed live
        # 2026-07-20 via 400 responses — not documented together anywhere):
        # wait_for_selector maxes at 15000ms, wait_for_navigation at 10000ms.
        # A too-high value 400s the whole request before it reaches the
        # target, so these aren't tunable upward if 10-15s isn't enough.
        {"wait_for_selector": {"selector": "body", "timeout": 15000}},
        {"execute": fill_js},
        {"wait": 500},
        {"execute": click_search_js},
        {"wait_for_navigation": {"timeout": 10000}},
    ]


# ── Result parsing ───────────────────────────────────────────────────────


def _parse_results_rows(html: str) -> list[dict]:
    """Extract case rows (case_number, caption, filed_date, detail_url) from
    a Smart Search results page.

    Odyssey Portal results are typically rendered as a data grid rather than
    a plain <table> (the same surprise extract_market_finder.py documented
    for DataSift's Market Finder) — this parser tries a generic <table> row
    scan first, falling back to a div-grid scan by row-like class names.
    Calibrate against a real --inspect dump before trusting this.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []

    for tr in soup.select("table tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        row_text = tr.get_text(" ", strip=True)
        link = tr.find("a", href=True)
        if not link:
            continue
        rows.append({
            "caption": row_text,
            "detail_url": link["href"],
        })

    if rows:
        return rows

    # Fallback: div-based grid rows (class name containing "row" or "result")
    for div in soup.select("[class*=Row], [class*=row], [class*=Result], [class*=result]"):
        link = div.find("a", href=True)
        if not link:
            continue
        row_text = div.get_text(" ", strip=True)
        if len(row_text) < 10:
            continue
        rows.append({
            "caption": row_text,
            "detail_url": link["href"],
        })

    return rows


def _resolve_detail_url(base_url: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://portal-nc.tylertech.cloud{href}"
    return f"{base_url.rsplit('/', 1)[0]}/{href}"


# ── Main per-county search ───────────────────────────────────────────────


async def run_ecourts_search(
    county: str,
    days_back: int,
    max_notices: int = 0,
    seen_case_numbers: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Search one county's Special Proceedings docket and scrape foreclosure cases."""
    if not is_target_ecourts_county(county):
        logger.warning("  %s is not a target eCourts county — skipping", county)
        return []

    logger.info("Running eCourts search: %s (last %d days)", county, days_back)
    session = f"ecourts_{county.lower()}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    scenario = _build_search_scenario(county, days_back)
    html = await _scrapfly_fetch(config.ECOURTS_SMART_SEARCH_URL, session=session, js_scenario=scenario)
    if not html:
        logger.error("  Could not load/search eCourts Portal for %s", county)
        return []

    rows = _parse_results_rows(html)
    logger.info("  %d case row(s) found for %s", len(rows), county)

    notices: list[NoticeData] = []
    for row in rows:
        if max_notices and len(notices) >= max_notices:
            break

        caption = row["caption"]
        if not is_foreclosure_special_proceeding(caption):
            logger.debug("  Skipping non-foreclosure SP: %s", caption[:80])
            continue

        m = re.search(r"\b(\d{2}\s?SP\s?\d{1,6})\b", caption, re.IGNORECASE)
        case_number = m.group(1).upper() if m else ""
        if seen_case_numbers is not None and case_number and case_number in seen_case_numbers:
            logger.info("  Skipping already-processed case %s", case_number)
            continue

        detail_url = _resolve_detail_url(config.ECOURTS_SMART_SEARCH_URL, row["detail_url"])
        case_html = await _scrapfly_fetch(detail_url, session=session)
        if not case_html:
            logger.warning("  Could not fetch case detail for %s", case_number or detail_url)
            continue

        case_text = BeautifulSoup(case_html, "html.parser").get_text("\n", strip=True)
        notice = parse_ecourts_case_text(
            case_text, county=county, source_url=detail_url, case_number_hint=case_number,
        )

        if seen_case_numbers is not None and case_number:
            seen_case_numbers[case_number] = notice.date_added or datetime.now().strftime("%Y-%m-%d")

        notices.append(notice)

    logger.info("  Found %d foreclosure SP notice(s) for %s", len(notices), county)
    return notices


async def scrape_all_ecourts(
    mode: str = "daily",
    counties: list[str] | None = None,
    days_back: int | None = None,
    max_notices: int = 0,
    seen_case_numbers: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Scrape the NC eCourts Portal for foreclosure Special Proceedings.

    Args:
        mode: "daily" (default 7-day window) or "historical" (default 365 days).
        counties: subset of config.ECOURTS_TARGET_COUNTIES, or None for all 5.
    """
    if counties is None:
        counties = config.ECOURTS_TARGET_COUNTIES
    if seen_case_numbers is None:
        seen_case_numbers = {}
    if days_back is None:
        days_back = 365 if mode == "historical" else 7

    all_notices: list[NoticeData] = []
    for county in counties:
        remaining = (max_notices - len(all_notices)) if max_notices else 0
        try:
            county_notices = await run_ecourts_search(
                county, days_back, max_notices=remaining, seen_case_numbers=seen_case_numbers,
            )
            all_notices.extend(county_notices)
        except Exception:
            logger.exception("Failed to scrape eCourts county: %s", county)

        if max_notices and len(all_notices) >= max_notices:
            break

    logger.info("Total eCourts notices scraped: %d", len(all_notices))
    return all_notices


# ── Manual DOM-inspection entry point ────────────────────────────────────


async def _inspect(county: str) -> None:
    """Fetch the raw Smart Search page via Scrapfly and dump it to disk for
    manual selector calibration. Run: python src/ecourts_scraper.py --inspect --county Wake
    """
    session = f"ecourts_inspect_{county.lower()}"
    html = await _scrapfly_fetch(config.ECOURTS_SMART_SEARCH_URL, session=session)
    if not html:
        print("Fetch failed — see logs above (likely ASP shield failure).")
        return
    out_path = config.OUTPUT_DIR / f"ecourts_inspect_{county.lower()}_{datetime.now().strftime('%Y%m%d%H%M%S')}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Dumped {len(html)} chars to {out_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="NC eCourts Portal scraper (dev/inspection tools)")
    parser.add_argument("--inspect", action="store_true", help="Dump raw Smart Search HTML for selector calibration")
    parser.add_argument("--county", type=str, default="Wake")
    args = parser.parse_args()

    if args.inspect:
        asyncio.run(_inspect(args.county))
    else:
        parser.print_help()
