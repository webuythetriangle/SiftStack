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

A merged notice is only re-surfaced in the day's output/upload CSV if
something material changed (auction date, sale price, tax delinquent
amount, decision maker, etc. — see `_MATERIAL_FIELDS`); otherwise it's a
pure re-notice of the same lead and is dropped from output while still
updating the registry's stored data.

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

# Fields whose change between a prior-known record and a freshly re-published
# notice indicates a genuine update worth re-surfacing (new auction date, a
# price/amount change, a new decision-maker, etc.) rather than the source
# simply re-listing the same notice under a new ID. Deliberately excludes
# AVM/enrichment fields (estimated_value, rvm_*, mls fluctuation, etc.) that
# get re-fetched every run and can drift slightly day-to-day on their own —
# those would defeat suppression by looking like a "change" on every re-notice.
_MATERIAL_FIELDS = {
    "auction_date",
    "owner_name",
    "tax_delinquent_amount",
    "tax_delinquent_years",
    "mls_last_sold_date",
    "mls_last_sold_price",
    "decedent_name",
    "decision_maker_name",
    "decision_maker_relationship",
    "date_of_death",
    "owner_deceased",
}


def _has_material_change(prior: NoticeData, merged: NoticeData) -> bool:
    """True if any material field differs between the prior record and the merged result."""
    return any(
        getattr(prior, f) != getattr(merged, f) for f in _MATERIAL_FIELDS
    )


# Subset of _MATERIAL_FIELDS that are parsed directly from the notice
# itself (no enrichment required) — safe to compare *before* running Zillow/
# NARRPR/obituary/entity-research. The rest of _MATERIAL_FIELDS (tax
# delinquent amount, last-sold price/date, decision maker) only exist after
# those enrichment steps run, so they can't be used for an early check.
_PRE_ENRICHMENT_FIELDS = {"auction_date", "owner_name", "decedent_name"}


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
) -> tuple[list[NoticeData], list[NoticeData], int, int]:
    """Merge each notice onto its previously-known record, if one exists.

    A re-published notice that matches a prior record but changes nothing
    material (see `_MATERIAL_FIELDS`) is dropped from the output list — it's
    the same lead, not a new one — but still merged into `all_merged` so the
    registry stays current (fresher source_url, freshly re-fetched
    enrichment, etc.) for the next comparison.

    Returns (output_notices, all_merged, updated_count, suppressed_count):
    - output_notices: what should actually be written to CSV / uploaded —
      new properties plus previously-known ones with a material change.
    - all_merged: every notice after merging, including suppressed repeats —
      pass this to `update_registry` so the registry reflects the latest data.
    - updated_count: how many matched an already-known property.
    - suppressed_count: how many of those had no material change and were dropped.
    """
    updated_count = 0
    suppressed_count = 0
    output_notices = []
    all_merged = []
    for notice in notices:
        key = property_key(notice)
        prior = registry.get(key)
        if prior:
            prior_notice = NoticeData(
                **{k: v for k, v in prior.items() if k in _NOTICE_FIELDS}
            )
            merged = merge_notice_data(base=prior_notice, overlay=notice)
            updated_count += 1
            all_merged.append(merged)
            if _has_material_change(prior_notice, merged):
                output_notices.append(merged)
            else:
                suppressed_count += 1
        else:
            all_merged.append(notice)
            output_notices.append(notice)
    return output_notices, all_merged, updated_count, suppressed_count


def precheck_unchanged(
    notices: list[NoticeData], registry: dict[str, dict]
) -> tuple[list[NoticeData], list[NoticeData]]:
    """Split off notices that are almost certainly an unchanged re-notice,
    *before* Zillow/NARRPR/obituary enrichment runs on them. (Entity research
    and tax delinquency enrichment run earlier in the pipeline, before Smarty
    address standardization, and are unaffected by this check — see the
    call site in enrichment_pipeline.py Step 6b.)

    Only compares `_PRE_ENRICHMENT_FIELDS` — the fields available straight
    from parsing — against the registry. A match there means the source
    re-published the exact same notice under a new ID; there's nothing new
    for Zillow/NARRPR/obituary to find, and running NARRPR in particular
    would sign the user out of their own account (single-session account,
    see narrpr_enricher.py) for zero new information.

    This is purely an optimization: notices in the returned "unchanged" list
    are already fully merged with their prior record (so all previously
    enriched data is retained), and will still pass through
    `reconcile_with_registry()` at Step 9a like everything else — that step
    remains the source of truth for what's actually new/changed/suppressed.
    A field mismatch, missing prior, or any doubt just sends the notice
    through full enrichment as normal.

    Returns (to_enrich, unchanged):
    - to_enrich: new properties, or ones whose pre-enrichment fields moved —
      must go through the full enrichment pipeline.
    - unchanged: already merged with their prior record; skip straight to
      Step 9a where they'll be suppressed from output as a duplicate.
    """
    to_enrich = []
    unchanged = []
    for notice in notices:
        key = property_key(notice)
        prior = registry.get(key)
        if prior and all(
            (getattr(notice, f) or "") == (prior.get(f) or "")
            for f in _PRE_ENRICHMENT_FIELDS
        ):
            prior_notice = NoticeData(
                **{k: v for k, v in prior.items() if k in _NOTICE_FIELDS}
            )
            unchanged.append(merge_notice_data(base=prior_notice, overlay=notice))
        else:
            to_enrich.append(notice)
    return to_enrich, unchanged


def update_registry(registry: dict[str, dict], notices: list[NoticeData]) -> None:
    """Upsert this run's final records into the registry, keyed by property."""
    for notice in notices:
        registry[property_key(notice)] = asdict(notice)
