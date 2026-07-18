"""NARRPR (RPR — Realtors Property Resource) RVM enrichment.

Pulls RVM (RPR Valuation Model) data for pipeline addresses via Playwright,
following the same "no public API, browser automation required" pattern as
datasift_core.py. Unlike DataSift, RPR enforces a single concurrent session
per account — logging in anywhere (including the user's own browser) signs
out every other session. That makes long-lived cookie persistence across
separate runs actively harmful (a fresh login invalidates the old session
mid-batch), so this module authenticates once per run and holds that single
browser session for the whole batch instead of persisting cookies to disk.

Once authenticated, all property lookups are done via direct HTTP calls to
RPR's internal JSON API (webapi.narrpr.com) using the OIDC bearer token
extracted from the `oidc.at` cookie — no further page navigation needed.
This was verified against real account data (see CLAUDE.md "NARRPR RVM
Enrichment Patterns" section for how the endpoints were reverse-engineered).

The bearer token expires after ~1 hour. Batches that could run longer than
that should be chunked externally; this module does not attempt token
refresh.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import BrowserContext, async_playwright

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

NARRPR_HOME_URL = "https://www.narrpr.com/"
GEOCODE_URL = "https://webapi.narrpr.com/misc/location-suggestions"
COMMON_URL_TMPL = "https://webapi.narrpr.com/properties/{property_id}/common"
DETAILS_URL_TMPL = "https://webapi.narrpr.com/properties/{property_id}/details"

# Knoxville, TN centroid — biases RPR's fuzzy address matching toward the
# correct market. Harmless for other states; RPR's geocoder still matches on
# the full address string, this just weights ambiguous/partial matches.
KNOX_LATITUDE = 35.9606
KNOX_LONGITUDE = -83.9207

# Modals that can block interaction after login: cookie consent, Beamer
# announcements, and RPR's own "another user detected" single-session notice.
_DISMISS_BUTTON_TEXTS = ["Close", "Got it", "Dismiss", "No Thanks"]


@dataclass
class RvmResult:
    """RVM valuation data for a single property."""
    rvm_value: Optional[int] = None
    rvm_value_low: Optional[int] = None
    rvm_value_high: Optional[int] = None
    rvm_confidence: Optional[int] = None
    rvm_updated_date: str = ""


class NarrprSession:
    """One authenticated NARRPR browser session, reused across many lookups.

    Usage:
        session = NarrprSession()
        await session.start()
        try:
            result = await session.lookup_rvm("123 Main St, Knoxville, TN")
        finally:
            await session.close()
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._token: str = ""

    async def start(self) -> None:
        email = config.NARRPR_EMAIL
        password = config.NARRPR_PASSWORD
        if not email or not password:
            raise ValueError("NARRPR_EMAIL and NARRPR_PASSWORD must be set in .env")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(viewport={"width": 1440, "height": 900})
        page = await self._context.new_page()

        logger.info("Logging into NARRPR ...")
        await page.goto(NARRPR_HOME_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.get_by_placeholder("user@gmail.com").first.fill(email)
        await page.locator('input[type="password"]').first.fill(password)
        await page.wait_for_timeout(300)
        await page.locator("#SignInBtn").first.click(force=True)
        await page.wait_for_url("**/home", timeout=20000)

        for text in _DISMISS_BUTTON_TEXTS:
            btn = page.locator(f'button:has-text("{text}")')
            if await btn.count() > 0:
                try:
                    await btn.first.click(timeout=2000)
                except Exception:
                    pass

        cookies = await self._context.cookies()
        token = next((c["value"] for c in cookies if c["name"] == "oidc.at"), None)
        if not token:
            raise RuntimeError("NARRPR login succeeded but no session token was found")
        self._token = token
        await page.close()
        logger.info("NARRPR session established")

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _headers(self) -> dict:
        return {
            "authorization": f"Bearer {self._token}",
            "accept": "application/json, text/plain, */*",
            "rpr-referrer": "https://www.narrpr.com/home",
        }

    async def lookup_rvm(self, address: str) -> Optional[RvmResult]:
        """Resolve a free-text address to RPR's RVM valuation.

        Returns None if the address can't be geocoded to a property, or if
        RPR has no RVM estimate for that property (common for vacant land
        and commercial/special-purpose buildings).
        """
        if not self._context:
            raise RuntimeError("NarrprSession.start() must be called before lookup_rvm()")

        geo_resp = await self._context.request.get(
            GEOCODE_URL,
            params={
                "propertyMode": "1",
                "userQuery": address,
                "userLatitude": str(KNOX_LATITUDE),
                "userLongitude": str(KNOX_LONGITUDE),
                "category": "1",
                "getPlacesAreasAndProperties": "true",
                "getStreets": "false",
                "getListingIdsApnsAndTaxIds": "false",
                "getSchools": "false",
            },
            headers=self._headers(),
        )
        if geo_resp.status != 200:
            logger.warning("NARRPR geocode failed (%s) for %r", geo_resp.status, address)
            return None
        geo_json = await geo_resp.json()
        locations = geo_json.get("sections", [{}])[0].get("locations", [])
        if not locations:
            logger.debug("NARRPR: no property match for %r", address)
            return None
        property_id = locations[0]["propertyId"]

        common_resp = await self._context.request.get(
            COMMON_URL_TMPL.format(property_id=property_id),
            params={"preferredPropertyMode": "1"},
            headers=self._headers(),
        )
        if common_resp.status != 200:
            logger.warning("NARRPR common lookup failed (%s) for propertyId=%s", common_resp.status, property_id)
            return None
        common_json = await common_resp.json()

        details_resp = await self._context.request.get(
            DETAILS_URL_TMPL.format(property_id=property_id),
            params={
                "orgId": common_json.get("orgId", ""),
                "listingId": common_json.get("listingId", ""),
                "zipPlaceId": str(common_json.get("zipPlaceId", "")),
                "propertyMode": str(common_json.get("propertyMode", 1)),
                "sections": "43",
            },
            headers=self._headers(),
        )
        if details_resp.status != 200:
            logger.warning("NARRPR details lookup failed (%s) for propertyId=%s", details_resp.status, property_id)
            return None
        details_json = await details_resp.json()
        summary = details_json.get("summarySection", {})

        if not summary.get("hasEstimatedValue"):
            logger.debug("NARRPR: no RVM estimate available for %r", address)
            return None

        return RvmResult(
            rvm_value=common_json.get("estimatedValue"),
            rvm_value_low=summary.get("estimatedRangeFrom"),
            rvm_value_high=summary.get("estimatedRangeTo"),
            rvm_confidence=summary.get("estimatedValueConfidenceScore"),
            rvm_updated_date=summary.get("estimatedValueDate", ""),
        )


async def _enrich_batch(notices: list[NoticeData]) -> None:
    session = NarrprSession()
    await session.start()
    try:
        for n in notices:
            address = f"{n.address}, {n.city}, {n.state} {n.zip}".strip()
            try:
                result = await session.lookup_rvm(address)
            except Exception as e:
                logger.warning("  NARRPR lookup failed for %r: %s", address, e)
                result = None

            if result:
                if result.rvm_value is not None:
                    n.rvm_value = str(result.rvm_value)
                if result.rvm_value_low is not None:
                    n.rvm_value_low = str(result.rvm_value_low)
                if result.rvm_value_high is not None:
                    n.rvm_value_high = str(result.rvm_value_high)
                if result.rvm_confidence is not None:
                    n.rvm_confidence = str(result.rvm_confidence)
                if result.rvm_updated_date:
                    n.rvm_updated_date = result.rvm_updated_date

            # Conservative pacing — RPR is a NAR/MLS member benefit, not a
            # dedicated data API, so we deliberately rate-limit rather than
            # hammer it the way the tnpublicnotice scraper can.
            await asyncio.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))
    finally:
        await session.close()


def enrich_rvm_data(notices: list[NoticeData]) -> None:
    """Populate rvm_* fields on every notice with a usable address, in-place.

    Sync entry point for enrichment_pipeline.py — opens one NARRPR session
    for the whole batch and closes it when done.
    """
    candidates = [n for n in notices if n.address.strip() and n.city.strip() and n.state.strip()]
    if not candidates:
        return
    asyncio.run(_enrich_batch(candidates))
