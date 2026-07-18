"""Parse ncnotices.com (North Carolina) foreclosure notice text into NoticeData.

Mirrors notice_parser.py's extraction strategy (indicator-phrase address
matching, "commonly known as" / "located at" context, owner-name regexes) but
swaps every Tennessee-specific piece — state token, zip prefix range, city
list, target-county set — for North Carolina equivalents. The owner-name,
auction-date, and text-cleaning helpers are state-agnostic and are imported
directly from notice_parser.py rather than duplicated.

ncnotices.com's search results already carry structured City/County metadata
per notice (visible in the row's hidden `.right` div), so callers should pass
that through as city_hint/county rather than relying solely on regex-guessed
city extraction from the notice body.
"""

import logging
import re

from notice_parser import (
    NoticeData,
    _clean_address,
    _clean_city,
    _clean_name,
    _extract_publish_date,
    _is_valid_name,
    _normalize_date,
    _parse_auction_date,
    _parse_name,
    _PROP_INDICATOR,
    _ADDR_PART,
    _OPTIONAL_COUNTY,
)

logger = logging.getLogger(__name__)

# ── North Carolina target counties (Wake expansion market) ─────────────
NC_TARGET_COUNTIES = {"wake", "durham", "orange", "guilford", "mecklenburg"}

# ── NC cities across the 5 target counties (for city fallback scan) ────
NC_CITIES: list[str] = sorted(
    [
        "Raleigh", "Cary", "Apex", "Wake Forest", "Garner", "Knightdale",
        "Fuquay-Varina", "Morrisville", "Holly Springs", "Wendell", "Zebulon",
        "Rolesville", "Durham", "Chapel Hill", "Hillsborough", "Carrboro",
        "Mebane", "Greensboro", "High Point", "Jamestown", "Gibsonville",
        "Charlotte", "Matthews", "Huntersville", "Cornelius", "Davidson",
        "Pineville", "Mint Hill",
    ],
    key=len,
    reverse=True,
)
_KNOWN_CITIES_SET: set[str] = {c.title() for c in NC_CITIES}

# NC zip codes: 27xxx-28xxx range
NC_ZIP_RE = re.compile(r"\b(2[78]\d{3})(?:-\d{4})?\b")

# Zips to reject when found via fallback (courthouse/law-office boilerplate)
_NC_COURTHOUSE_ZIPS = {
    "27601",  # Downtown Raleigh (Wake County Courthouse / govt offices)
    "27701",  # Downtown Durham (courthouse area)
    "27401",  # Downtown Greensboro (courthouse area)
    "28202",  # Downtown Charlotte (courthouse area)
}

_NC_COUNTY_ZIP_PREFIXES: dict[str, list[str]] = {
    "Wake": ["275", "276"],
    "Durham": ["277"],
    "Orange": ["275", "273", "277"],
    "Guilford": ["274"],
    "Mecklenburg": ["282", "281", "280"],
}

# ── NC property-address regexes (same building blocks as notice_parser.py,
# with North Carolina / NC swapped in for Tennessee / TN) ──────────────
NC_FULL_PROPERTY_RE = re.compile(
    _PROP_INDICATOR
    + r"\s*[:.,\s]*"
    + _ADDR_PART
    + r"(?:\s*[,.]?\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?"
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + r"(?:North\s+Carolina|N\.C\.?|NC)"
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)

NC_PROPERTY_ADDR_RE = re.compile(
    _PROP_INDICATOR + r"\s*[:.,\s]*" + _ADDR_PART,
    re.IGNORECASE,
)

NC_LOCATED_AT_FULL_RE = re.compile(
    r"located\s+at\s+"
    + _ADDR_PART
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + r"(?:North\s+Carolina|N\.C\.?|NC)"
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)

NC_LOCATED_AT_ADDR_RE = re.compile(
    r"located\s+at\s+" + _ADDR_PART,
    re.IGNORECASE,
)

_BAD_ADDR_WORDS = [
    "courthouse", "court house", "county building", "city building",
    "city county", "register", "office of", "entrance",
    "county court", "usual and customary", "main entrance",
]


def _is_valid_address(addr: str) -> bool:
    if not addr or len(addr.strip()) < 5:
        return False
    lower = addr.lower()
    for bad in _BAD_ADDR_WORDS:
        if bad in lower:
            return False
    m = re.match(r"(\d+)", addr)
    if m:
        num = int(m.group(1))
        if num < 1 or num > 99999:
            return False
    return True


def _extract_city_zip_near_nc(notice: NoticeData, text: str, addr_end: int) -> None:
    """Extract city and zip from the text near the end of an NC address match."""
    window = text[addr_end:addr_end + 200]

    city_state_re = re.compile(
        r"[,.\s]+([\w][\w\s]*?)"
        r"(?:\s*[,.]\s*\w+\s+County)?"
        r"\s*[,.]\s*(?:North\s+Carolina|N\.C\.?|NC)"
        r"\s*[,.\s]*(\d{5}(?:-\d{4})?)?",
        re.IGNORECASE,
    )
    m = city_state_re.match(window)
    if m:
        notice.city = _clean_city(m.group(1))
        if m.group(2):
            notice.zip = m.group(2)
        return

    window_upper = window.upper()
    for city in NC_CITIES:
        if city.upper() in window_upper:
            notice.city = city
            break

    zip_match = NC_ZIP_RE.search(window)
    if zip_match:
        notice.zip = zip_match.group(1)


def is_target_nc_county(county: str) -> bool:
    """Check the county (already known from the search row metadata) is in scope."""
    return county.strip().lower() in NC_TARGET_COUNTIES


def _parse_address_nc(notice: NoticeData) -> None:
    """Extract property address/city/zip from NC notice body text.

    Same priority order as notice_parser._parse_address, with NC state tokens.
    """
    text = notice.raw_text.replace("\xa0", " ")

    m = NC_FULL_PROPERTY_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        if _is_valid_address(addr):
            notice.address = addr
            notice.city = _clean_city(m.group(2))
            if m.group(3):
                notice.zip = m.group(3)
            return

    m = NC_PROPERTY_ADDR_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        if _is_valid_address(addr):
            notice.address = addr
            _extract_city_zip_near_nc(notice, text, m.end())
            return

    m = NC_LOCATED_AT_FULL_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        context_before = text[max(0, m.start() - 80):m.start()].lower()
        is_sale_location = any(
            w in context_before for w in ["sale", "auction", "held", "entrance", "courthouse"]
        )
        if not is_sale_location and _is_valid_address(addr):
            notice.address = addr
            notice.city = _clean_city(m.group(2))
            if m.group(3):
                notice.zip = m.group(3)
            return

    m = NC_LOCATED_AT_ADDR_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        context_before = text[max(0, m.start() - 80):m.start()].lower()
        is_sale_location = any(
            w in context_before for w in ["sale", "auction", "held", "entrance", "courthouse"]
        )
        if not is_sale_location and _is_valid_address(addr):
            notice.address = addr
            _extract_city_zip_near_nc(notice, text, m.end())
            return


def _extract_notice_content(full_text: str) -> str:
    """Pull just the notice body from the detail page's full text.

    ncnotices.com runs the same WebStrides platform as tnpublicnotice.com, so
    the "Notice Content" label is expected post-Turnstile-solve. Falls back to
    the raw full page text (minus footer boilerplate) if that label isn't found,
    since this hasn't been confirmed against a live solved page yet.
    """
    marker = "Notice Content"
    idx = full_text.find(marker)
    if idx == -1:
        # Fallback: trim known boilerplate (nav, T&C, language picker) and
        # keep the rest — better than returning nothing.
        body = full_text
        start_marker = "Please Note:"
        start_idx = body.find(start_marker)
        if start_idx != -1:
            body = body[start_idx:]
        for end_marker in ["\nBack\n", "\nIf you have any questions", "\nSelect Language"]:
            end_idx = body.find(end_marker)
            if end_idx != -1:
                body = body[:end_idx]
                break
        return body.strip()

    body = full_text[idx + len(marker):]
    for end_marker in ["\nBack\n", "\nIf you have any questions", "\nSelect Language"]:
        end_idx = body.find(end_marker)
        if end_idx != -1:
            body = body[:end_idx]
            break
    return body.strip()


def parse_nc_notice_text(
    full_text: str,
    county: str,
    notice_type: str,
    source_url: str,
    city_hint: str = "",
    date_added: str = "",
) -> NoticeData:
    """Build a NoticeData from a solved ncnotices.com detail page's full text.

    city_hint should come from the search result row's structured "City: X"
    metadata (more reliable than regex-guessing from the notice body).
    """
    notice = NoticeData(
        county=county,
        notice_type=notice_type,
        source_url=source_url,
        state="NC",
    )

    full_text = full_text.replace("\xa0", " ")
    notice_content = _extract_notice_content(full_text)
    notice.raw_text = notice_content if notice_content else full_text

    if not notice.raw_text.strip():
        logger.warning("No notice text found on %s", source_url)
        return notice

    notice.date_added = date_added or _extract_publish_date(full_text)

    _parse_address_nc(notice)
    if city_hint and not notice.city:
        notice.city = city_hint
    _parse_name(notice)
    if notice_type != "probate":
        _parse_auction_date(notice)

    return notice
