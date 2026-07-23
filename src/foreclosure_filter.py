"""Classify foreclosure notices — keep only first-to-market trustee sales.

Title variations observed on tnpublicnotice.com (Feb 2026):
  - SUBSTITUTE TRUSTEE'S NOTICE OF SALE
  - SUBSTITUTE TRUSTEE'S SALE
  - SUBSTITUTE TRUSTEE'S NOTICE OF FORECLOSURE SALE
  - SUCCESSOR TRUSTEE'S NOTICE OF SALE OF REAL ESTATE
  - NOTICE OF TRUSTEE'S SALE
  - NOTICE OF TRUSTEE'S FORECLOSURE SALE
  - NOTICE OF SUBSTITUTE TRUSTEE'S SALE
  - NOTICE OF DEFAULT AND FORECLOSURE SALE
  - FORECLOSURE SALE NOTICE
"""

import logging

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# ── Inclusion keywords ─────────────────────────────────────────────────
# These phrases identify real first-to-market foreclosure / trustee sale notices.
# Matched case-insensitively against the full notice text.
INCLUDE_PHRASES = [
    # Substitute trustee variants
    "substitute trustee's notice of sale",
    "substitute trustee's sale",
    "substitute trustee's notice of foreclosure sale",
    "substitute trustee sale",
    "substituted trustee's sale",
    "substituted trustee sale",
    "notice of substitute trustee's sale",
    "notice of substitute trustee sale",
    # Successor trustee
    "successor trustee's notice of sale",
    "successor trustee's sale",
    "successor trustee sale",
    # General trustee sale
    "notice of trustee's sale",
    "notice of trustee's foreclosure sale",
    "notice of trustee sale",
    "trustee's sale",
    "trustee sale",
    # Default / foreclosure sale (with trustee guard below)
    "notice of default and foreclosure sale",
    "foreclosure sale notice",
    "notice of foreclosure sale",
    # Generic — only accepted if "trustee" also appears
    "notice of sale",
]

# ── Exclusion keywords ─────────────────────────────────────────────────
# These override inclusion — the notice is NOT a first-to-market foreclosure.
EXCLUDE_PHRASES = [
    "non-resident notice",
    "non resident notice",
    "nonresident notice",
    "order of publication",
    "notice to creditors",
    "notice of lien",
    "order to sell",
    "divorce",
    "dissolution",
]


def is_valid_foreclosure(notice: NoticeData) -> bool:
    """Determine if a foreclosure notice is a real first-to-market trustee sale.

    Non-foreclosure notice types (tax_sale, tax_lien, probate) always pass through.
    """
    if notice.notice_type != "foreclosure":
        return True  # Non-foreclosure notices pass through unfiltered

    text = notice.raw_text.lower()

    if not text:
        logger.debug("Excluded foreclosure (empty text): %s", notice.source_url)
        return False

    # Check exclusions first — they take priority
    for phrase in EXCLUDE_PHRASES:
        if phrase in text:
            logger.debug("Excluded foreclosure (matched '%s'): %s", phrase, notice.source_url)
            return False

    # Check for inclusion phrases
    for phrase in INCLUDE_PHRASES:
        if phrase in text:
            # "notice of sale" alone isn't specific enough —
            # must also mention a trustee somewhere in the text
            if phrase == "notice of sale" and "trustee" not in text:
                continue
            return True

    # No inclusion phrase matched — exclude by default
    logger.debug("Excluded foreclosure (no trustee sale language): %s", notice.source_url)
    return False


# ── Tax foreclosure classification ──────────────────────────────────────
# A judicial "in rem"/in-personam tax foreclosure (county/city suing a
# delinquent-tax owner for a court Judgment, case captioned e.g. "County of
# Durham and City of Durham vs. Estate of X and Heirs") is a legally distinct
# process from the power-of-sale trustee foreclosure above — no deed of
# trust, no trustee, no substitute-trustee language. ncnotices.com's single
# "foreclosure" keyword search returns both kinds mixed together; this is
# only ever checked against notices that already failed is_valid_foreclosure,
# so a genuine trustee sale can never be misclassified here.
TAX_FORECLOSURE_PHRASES = [
    "tax foreclosure",
    "in rem tax foreclosure",
    "foreclosure of tax lien",
    "foreclosure of the tax lien",
    "foreclosure of tax liens",
]


def is_tax_foreclosure(notice: NoticeData) -> bool:
    """Detect a judicial tax foreclosure sale notice (county/city vs. delinquent owner)."""
    text = notice.raw_text.lower()
    if not text:
        return False
    return any(phrase in text for phrase in TAX_FORECLOSURE_PHRASES)
