"""Cross-run property-level lead registry.

Notice sources (tnpublicnotice.com, ncnotices.com) frequently re-publish an
updated or amended notice for a property that was already scraped — a
postponed foreclosure sale re-notice, an amended probate filing, etc. Each
re-publish gets a new notice ID, so the existing ID-based dedup in
data_formatter.deduplicate() treats it as a brand-new lead and it ends up
duplicated in the output CSV / DataSift.

This module tracks properties (not notice IDs) across runs, keyed by
address + zip + notice_type + county, and merges a newly-scraped notice
onto the previously-known record for that property instead of letting it
appear as a second lead. Merge is field-by-field: the newer notice's
non-empty fields win, and anything it doesn't have (e.g. Zillow/NARRPR/
decision-maker data from a prior run's enrichment) falls back to the
previously-known value rather than being lost.

Different notice_type for the same address (e.g. a probate followed later
by a foreclosure) is treated as a distinct event, not merged — those are
legitimately different distress signals worth tracking separately.
"""

import logging
from dataclasses import asdict, fields
from datetime import datetime, timedelta

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

_NOTICE_FIELDS = {f.name for f in fields(NoticeData)}


def property_key(notice: NoticeData) -> str:
    """Normalized key identifying a specific property + notice type + county.

    Not a full USPS-standardization — just enough normalization (case,
    whitespace) that the same property parsed slightly differently by two
    notices still matches. Addresses are usually Smarty-standardized by the
    time this is called (pipeline Step 6 runs first), which makes matching
    far more reliable than raw scraped text.
    """
    addr = " ".join((notice.address or "").strip().upper().split())
    zip5 = (notice.zip or "").strip()[:5]
    return f"{addr}|{zip5}|{notice.notice_type}|{notice.county.strip().lower()}"


def merge_notice_data(base: NoticeData, overlay: NoticeData) -> NoticeData:
    """Merge overlay onto base: overlay's non-empty fields win, else base's.

    Used both to collapse same-run duplicate notices for one property and to
    reconcile a freshly-scraped notice against a previously-exported record.
    """
    merged = {
        f.name: (getattr(overlay, f.name) or getattr(base, f.name))
        for f in fields(NoticeData)
    }
    return NoticeData(**merged)


def load_registry() -> dict[str, dict]:
    """Load the persisted property registry, pruning stale entries."""
    registry = config.load_state(config.SEEN_PROPERTIES_FILE)
    cutoff = (
        datetime.now() - timedelta(days=config.SEEN_PROPERTIES_PRUNE_DAYS)
    ).strftime("%Y-%m-%d")
    pruned = {
        k: v for k, v in registry.items() if v.get("date_added", "") >= cutoff
    }
    removed = len(registry) - len(pruned)
    if removed:
        logger.info(
            "Property registry: pruned %d stale entries (older than %d days)",
            removed,
            config.SEEN_PROPERTIES_PRUNE_DAYS,
        )
    return pruned


def save_registry(registry: dict[str, dict]) -> None:
    config.save_state(config.SEEN_PROPERTIES_FILE, registry)


def reconcile_with_registry(
    notices: list[NoticeData], registry: dict[str, dict]
) -> tuple[list[NoticeData], int]:
    """Merge each notice onto its previously-known record, if one exists.

    Returns the reconciled notices list and a count of how many were updates
    to an already-known property (as opposed to brand new).
    """
    updated_count = 0
    result = []
    for notice in notices:
        key = property_key(notice)
        prior = registry.get(key)
        if prior:
            prior_notice = NoticeData(
                **{k: v for k, v in prior.items() if k in _NOTICE_FIELDS}
            )
            notice = merge_notice_data(base=prior_notice, overlay=notice)
            updated_count += 1
        result.append(notice)
    return result, updated_count


def update_registry(registry: dict[str, dict], notices: list[NoticeData]) -> None:
    """Upsert this run's final records into the registry, keyed by property."""
    for notice in notices:
        registry[property_key(notice)] = asdict(notice)
