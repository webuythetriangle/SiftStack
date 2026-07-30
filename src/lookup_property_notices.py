"""One-off diagnostic tool: look up ALL ncnotices.com foreclosure notices for
a specific list of addresses (not the usual keyword/date-range saved
search), to recover the true first-notice date for properties whose merged
date_added got overwritten by a later re-notice — see the merge_notice_data
fix in property_registry.py (2026-07-29) for why this happened. Existing
scraped-and-merged data can't be repaired retroactively (the raw per-notice
history was discarded at merge time); this re-fetches it straight from the
source for just the affected addresses instead of re-scraping whole
counties/quarters.

Not part of the regular scrape pipeline — run manually against a short list
of known-bad properties.

Usage:
    python src/lookup_property_notices.py --input Guilford_bad_date_cases.csv \
        --county Guilford --output output/property_notice_lookup.csv
"""

import argparse
import asyncio
import csv
import logging
from pathlib import Path

from playwright.async_api import async_playwright

from config import NCSearch
from ncnotices_scraper import run_nc_search

logger = logging.getLogger(__name__)

RESULT_FIELDS = [
    "match_count", "true_first_notice", "notes",
    "matched_date_added", "matched_auction_date", "matched_owner_name",
    "matched_notice_type", "matched_source_url",
]


async def lookup_addresses(rows: list[dict], county: str, days_back: int) -> list[dict]:
    results: list[dict] = []

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

        for row in rows:
            address = row["address"].strip()
            if not address:
                continue
            search = NCSearch(county, "foreclosure", address)
            logger.info("Looking up: %s", address)
            try:
                # require_all_words=True: an address is a set of tokens that
                # must ALL appear (the default "Any Words" OR-matching used
                # for "foreclosure trustee" would match every notice
                # containing just "Drive" or the ZIP).
                notices = await run_nc_search(
                    page, search, days_back, seen_ids={}, require_all_words=True,
                )
            except Exception:
                logger.exception("Lookup failed for %s", address)
                notices = []

            if not notices:
                results.append({**row, "match_count": 0, "notes": "NO MATCHES"})
                continue

            dates = sorted(n.date_added for n in notices if n.date_added)
            true_first = dates[0] if dates else ""
            for n in notices:
                results.append({
                    **row,
                    "match_count": len(notices),
                    "true_first_notice": true_first,
                    "matched_date_added": n.date_added,
                    "matched_auction_date": n.auction_date,
                    "matched_owner_name": n.owner_name,
                    "matched_notice_type": n.notice_type,
                    "matched_source_url": n.source_url,
                })

        await browser.close()

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="CSV with an 'address' column")
    parser.add_argument("--county", default="Guilford")
    parser.add_argument("--days-back", type=int, default=365, help="Lookback window (site only has trailing 12mo anyway)")
    parser.add_argument("--output", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"No rows in {args.input}")

    results = asyncio.run(lookup_addresses(rows, args.county, args.days_back))

    out_path = Path(args.output) if args.output else Path("output") / "property_notice_lookup.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) + RESULT_FIELDS
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(results)

    no_match = sum(1 for r in results if r.get("match_count") == 0)
    logger.info("Wrote %d rows (%d addresses with no matches) to %s", len(results), no_match, out_path)


if __name__ == "__main__":
    main()
