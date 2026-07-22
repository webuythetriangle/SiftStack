"""Parse NC eCourts (Odyssey Portal) Special Proceedings case text into NoticeData.

Foreclosures under a power-of-sale deed of trust are filed as Special
Proceedings (NCGS Chapter 45, Article 2A) — the trustee/substitute trustee
files a "Notice of Hearing" with the Clerk of Superior Court. Statewide AOC
forms (e.g. AOC-CVM-812) use a fixed caption format regardless of county or
portal version:

    STATE OF NORTH CAROLINA          IN THE GENERAL COURT OF JUSTICE
    COUNTY OF [COUNTY]               SUPERIOR COURT DIVISION
                                      BEFORE THE CLERK
                                      FILE NO: [YY] SP [###]
    IN THE MATTER OF THE FORECLOSURE OF A DEED OF TRUST EXECUTED BY
    [GRANTOR NAME(S)], DATED [DATE], RECORDED IN BOOK [X] AT PAGE [Y] OF THE
    [COUNTY] COUNTY REGISTRY
                        NOTICE OF HEARING

Because that "...DEED OF TRUST EXECUTED BY [GRANTOR]..." caption language is
the same "executed by" convention already handled by notice_parser.EXECUTED_BY_RE
for tnpublicnotice.com/ncnotices.com's published sale notices, owner-name
extraction is NOT reimplemented here — _parse_name() from notice_parser.py is
reused directly. Likewise property-address extraction (when the form states
one — see NOT YET LIVE-VERIFIED note below) is state-specific, not
source-specific, so nc_notice_parser._parse_address_nc() is reused as-is.

What IS new here: case-type filtering (Special Proceedings has many subtypes
besides foreclosure — adoptions, guardianships, partitions, judicial sales,
name changes, involuntary commitments — this module's job is to keep only
foreclosure SPs) and case metadata extraction (case number, filing/hearing
dates) that doesn't exist in the newspaper-notice sources.

NOT YET LIVE-VERIFIED: portal-nc.tylertech.cloud returned an AWS WAF "Human
Verification" challenge on every unauthenticated navigation attempt during
initial testing (2026-07-19), and Scrapfly's ASP bypass did not clear the
shield on the first live attempt either (ERR::ASP::SHIELD_PROTECTION_FAILED).
The regexes below are built from the statewide-standard AOC form language
(stable across counties/years since it's dictated by statute/form, not by
portal UI) rather than from a captured live document. Calibrate
CASE_NUMBER_RE / HEARING_DATE_RE / the notice-content boundary markers
against a real fetched case document before trusting this in production —
see nc_detail.txt in the repo root for the pattern of how nc_notice_parser.py
was hand-tuned against a real captured page.
"""

import logging
import re

from notice_parser import (
    NoticeData,
    _clean_name,
    _is_valid_name,
    _normalize_date,
    _parse_name,
)

# The statewide AOC caption format reads "EXECUTED BY [GRANTOR], DATED
# [DATE], RECORDED IN BOOK..." — the comma is immediately followed by "DATED",
# which notice_parser.EXECUTED_BY_RE doesn't treat as a stop word (it was
# built for newspaper notices' "...,conveying..."/"...to [TRUSTEE]..."
# phrasing instead), so it fails to match eCourts captions at all. This
# caption-specific pattern is tried first; _parse_name() is the fallback.
ECOURTS_GRANTOR_RE = re.compile(
    r"executed\s+by\s+([A-Z][A-Za-z\s.,'-]+?)\s*,\s*dated",
    re.IGNORECASE,
)
from nc_notice_parser import _parse_address_nc, NC_TARGET_COUNTIES

logger = logging.getLogger(__name__)

# ── Case type filtering ─────────────────────────────────────────────────
# Special Proceedings caption must reference a deed of trust / mortgage
# foreclosure — everything else in the SP docket (adoptions, partitions,
# guardianships, name changes, judicial sales unrelated to a deed of trust,
# involuntary commitments, notary bonds) is excluded.
FORECLOSURE_CAPTION_RE = re.compile(
    r"foreclosure\s+of\s+a?\s*(?:deed\s+of\s+trust|mortgage)",
    re.IGNORECASE,
)

_SP_EXCLUDE_PHRASES = [
    "adoption",
    "guardianship",
    "incompetency",
    "involuntary commitment",
    "partition",
    "name change",
    "notary",
    "judicial sale",  # only excluded when NOT also matching FORECLOSURE_CAPTION_RE
]


def is_foreclosure_special_proceeding(case_caption: str) -> bool:
    """Determine whether a Special Proceedings case is a deed-of-trust foreclosure.

    `case_caption` should be the case title/description text as shown in the
    Smart Search results row or case detail header — NOT the full filing body.
    """
    if not case_caption:
        return False
    text = case_caption.lower()

    if FORECLOSURE_CAPTION_RE.search(text):
        return True

    for phrase in _SP_EXCLUDE_PHRASES:
        if phrase in text:
            return False

    return False


# ── Case metadata ────────────────────────────────────────────────────────
# NC AOC file number format: "26 SP 123" (2-digit year, county-suffixed on
# some portal views e.g. "26SP123-410" where 410 is the county FIPS code).
CASE_NUMBER_RE = re.compile(r"\b(\d{2}\s?SP\s?\d{1,6}(?:-\d{3})?)\b", re.IGNORECASE)

# "A hearing will be held on March 12, 2026 at 9:00 AM" / "Date of Hearing: ..."
HEARING_DATE_RE = re.compile(
    r"(?:hearing\s+(?:will\s+be\s+held|is\s+scheduled)\s+(?:on|for)|date\s+of\s+hearing:?)\s*"
    r"(\w+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# "FILED: March 1, 2026" / "Filing Date: ..." — when the case metadata panel
# (rather than the notice body) is the source text.
FILED_DATE_RE = re.compile(
    r"(?:filed|filing\s+date)\s*:?\s*(\w+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def _extract_case_number(text: str) -> str:
    m = CASE_NUMBER_RE.search(text)
    return m.group(1).upper().replace(" ", " ") if m else ""


def _extract_hearing_date(text: str) -> str:
    m = HEARING_DATE_RE.search(text)
    return _normalize_date(m.group(1)) if m else ""


def _extract_filed_date(text: str) -> str:
    m = FILED_DATE_RE.search(text)
    return _normalize_date(m.group(1)) if m else ""


def is_target_ecourts_county(county: str) -> bool:
    return county.strip().lower() in NC_TARGET_COUNTIES


def parse_ecourts_case_text(
    full_text: str,
    county: str,
    source_url: str,
    case_number_hint: str = "",
) -> NoticeData:
    """Build a NoticeData from a Special Proceedings foreclosure case's text.

    `full_text` is expected to be the case detail page / Notice of Hearing
    filing body — whatever the eCourts scraper resolves for a given case
    (the exact extraction point — register-of-actions text vs. a linked PDF —
    is still being calibrated; see module docstring).
    """
    notice = NoticeData(
        county=county,
        notice_type="foreclosure",
        source_url=source_url,
        state="NC",
    )

    full_text = full_text.replace("\xa0", " ")
    notice.raw_text = full_text

    if not full_text.strip():
        logger.warning("No case text found for %s", source_url)
        return notice

    case_number = case_number_hint or _extract_case_number(full_text)
    filed_date = _extract_filed_date(full_text)
    notice.date_added = filed_date or _extract_hearing_date(full_text)

    if case_number:
        logger.debug("Parsed eCourts case %s (%s)", case_number, source_url)

    _parse_address_nc(notice)

    m = ECOURTS_GRANTOR_RE.search(full_text)
    if m:
        name = _clean_name(m.group(1))
        if _is_valid_name(name):
            notice.owner_name = name
    if not notice.owner_name:
        _parse_name(notice)

    return notice
