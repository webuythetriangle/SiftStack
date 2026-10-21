"""Screen raw NC county tax-delinquent exports for deceased owners + deal criteria.

Takes a raw tax-delinquent data source for one of the 4 NC counties this
project already tracks delinquency for at the notice-matching level
(nc_tax_enricher.py — Wake, Durham, Orange, Guilford; Mecklenburg has no
automatable source, see that module's docstring) and runs it the other
direction: instead of matching scraped notices against county delinquency
data, this canvasses the FULL delinquent list itself.

Pipeline:
  1. Load/fetch the county's raw delinquent records, grouped per property
     (owner name(s), address, years delinquent, total amount due, taxable
     value).
  2. Apply the financial screen: >=2 years unpaid, >=$2,500 total owed,
     >=$100,000 taxable value. Cheap and local, so it runs BEFORE the
     obituary canvas to avoid burning web/LLM calls on records that would
     be excluded anyway — the deceased-owner flag doesn't change whether a
     record qualifies (both criteria apply independently to every record),
     so screening first vs. after doesn't change the final output, only
     the cost of producing it.
  3. Canvas the screened owners for deceased status via obituary_enricher
     (lean mode: confirms deceased + date of death + obituary URL only —
     no heir/executor/address lookup).
  4. Write one combined CSV.

Per-county data source verified live 2026-08-18 (see docstrings on each
fetch function) — same verification standard as nc_tax_enricher.py.

KNOWN GAPS (fail closed, not guessed):
  - Durham's delinquent-list endpoint has no taxable-value field, and no
    second endpoint for it was found live (see fetch_durham_records).
    Durham records always have a blank taxable_value and therefore never
    pass the $100k+ screen from this source alone.
  - Orange's annual bulk file has no per-year breakdown (a known limitation
    already documented in nc_tax_enricher.py) — years_unpaid is unknown for
    every Orange record, so Orange records never pass the 2+-years screen
    from this source alone.
  - Mecklenburg has no automated source at all (see nc_tax_enricher.py) and
    is not supported here either.
"""

import argparse
import csv
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
REQUEST_DELAY_MIN = 0.3
REQUEST_DELAY_MAX = 0.8
_HEADERS = {
    "User-Agent": "SiftStackBot/1.0 (real estate lead research; "
    "contact vishal@webuythetriangle.com)"
}

SCREEN_MIN_YEARS_UNPAID = 2
SCREEN_MIN_TOTAL_DUE = 2500.0
SCREEN_MIN_TAXABLE_VALUE = 100000.0

SUPPORTED_COUNTIES = {"wake", "durham", "orange", "guilford"}


@dataclass
class _ScreenStats:
    total: int = 0
    excluded_years: int = 0
    excluded_amount: int = 0
    excluded_value: int = 0
    excluded_missing_value: int = 0
    passed: int = 0


def _to_float(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(",", "").replace("$", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


# ── Wake County ──────────────────────────────────────────────────────────
# Bulk XLSX, same file nc_tax_enricher._wake_download() fetches
# (services.wake.gov/collection_extracts/REAL_ESTATE_delq853_MMDDYYYY.xlsx).
# One row per (account, tax year) bill. Confirmed live 2026-08-18 against a
# cached copy — has separate Street_Number/STREET_NAME columns (no address
# parsing needed) and, unlike the other 3 counties, the taxable value
# (NET_BILLABLE_VALUE) is right there in the same file as the delinquency
# amount — no second lookup required.

_WAKE_REQUIRED_COLS = {
    "ACCOUNT_NUM", "TAX_YEAR", "Street_Number", "STREET_NAME", "ST_TYPE",
    "CITY", "ZIP_CODE", "Primary_Owner", "TOTAL_DUE", "NET_BILLABLE_VALUE",
}


def parse_wake_bulk_file(path: Path) -> list[NoticeData]:
    """Parse Wake's raw delinquent-tax bulk export into per-property records."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}

    missing = _WAKE_REQUIRED_COLS - set(col)
    if missing:
        raise ValueError(f"Wake file missing expected columns: {missing}")

    by_account: dict[str, dict] = {}
    for row in rows:
        account = row[col["ACCOUNT_NUM"]]
        if not account:
            continue
        entry = by_account.setdefault(account, {
            "years": {},           # tax_year -> total_due for that year
            "primary_owner": row[col["Primary_Owner"]] or "",
            "secondary_owner": (row[col.get("Secondary_Owner", -1)] or "")
                                if "Secondary_Owner" in col else "",
            "house_number": str(row[col["Street_Number"]] or "").strip(),
            "street_prefix": str(row[col.get("Street_Prefix", -1)] or "").strip()
                              if "Street_Prefix" in col else "",
            "street": str(row[col["STREET_NAME"]] or "").strip(),
            "st_type": str(row[col["ST_TYPE"]] or "").strip(),
            "street_suffix": str(row[col.get("Street_Suffix", -1)] or "").strip()
                              if "Street_Suffix" in col else "",
            "city": str(row[col["CITY"]] or "").strip().title(),
            "zip": str(row[col["ZIP_CODE"]] or "").strip(),
            "reid": str(row[col.get("REID", -1)] or "").strip() if "REID" in col else "",
            "taxable_value": 0.0,
            "taxable_value_year": "",
        })
        year = str(row[col["TAX_YEAR"]] or "")
        due = _to_float(row[col["TOTAL_DUE"]])
        if due > 0 and year:
            entry["years"][year] = entry["years"].get(year, 0.0) + due
        # Taxable value can change year to year — keep the most recent one.
        value = _to_float(row[col["NET_BILLABLE_VALUE"]])
        if value <= 0 and "TOTAL_REAL_VALUE" in col:
            value = _to_float(row[col["TOTAL_REAL_VALUE"]])
        if value > 0 and year >= (entry["taxable_value_year"] or ""):
            entry["taxable_value"] = value
            entry["taxable_value_year"] = year

    notices: list[NoticeData] = []
    today = date.today().isoformat()
    for account, entry in by_account.items():
        street_parts = [
            entry["street_prefix"], entry["house_number"], entry["street"],
            entry["st_type"], entry["street_suffix"],
        ]
        address = " ".join(p for p in street_parts if p).strip()
        if not address:
            continue
        owner_name = entry["primary_owner"]
        if entry["secondary_owner"]:
            owner_name = f"{owner_name} & {entry['secondary_owner']}"

        n = NoticeData(
            date_added=today,
            address=address,
            city=entry["city"],
            state="NC",
            zip=entry["zip"],
            owner_name=owner_name,
            notice_type="tax_delinquent",
            county="Wake",
            source_url="https://services.wake.gov/collection_extracts",
            parcel_id=entry["reid"] or account,
            tax_delinquent_amount=str(round(sum(entry["years"].values()), 2)),
            tax_delinquent_years=str(len(entry["years"])),
            tax_owner_name=owner_name,
            taxable_value=str(round(entry["taxable_value"], 2)) if entry["taxable_value"] else "",
        )
        notices.append(n)

    logger.info("Wake: parsed %d delinquent properties from %s", len(notices), path.name)
    return notices


# ── Guilford County ──────────────────────────────────────────────────────
# Live 2-layer ArcGIS fetch — same two FeatureServers as
# nc_tax_enricher.py's Guilford path, but queried in the opposite direction
# (every delinquent parcel -> its address, not one known address -> its
# delinquency). Confirmed live 2026-08-18: the delinquency layer itself
# carries ABSTRACT_TAXABLE_VALUE right alongside the amount due, and the
# parcel layer carries the situs address (PHYADDR_*) — no address parsing
# needed for either.

GUILFORD_DELQ_URL = (
    "https://services5.arcgis.com/RR1v7NWFfwk98pUn/arcgis/rest/services/"
    "Tax_Delinquent_Report_/FeatureServer/0/query"
)
GUILFORD_PARCEL_URL = (
    "https://gcgis.guilfordcountync.gov/arcgis/rest/services/"
    "GC_Cadastral_Current/GC_Parcels/FeatureServer/0/query"
)


def _guilford_fetch_delinquent() -> list[dict]:
    features: list[dict] = []
    offset = 0
    page_size = 2000
    while True:
        params = {
            "where": "BILL_STATUS='UNPAID'",
            "outFields": "PARCEL_NUM,OWNER_NAME,TAX_YEAR,TOTAL_DUE_AMOUNT,"
                         "ABSTRACT_TAXABLE_VALUE",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        resp = requests.get(GUILFORD_DELQ_URL, params=params, timeout=REQUEST_TIMEOUT,
                             headers=_HEADERS)
        resp.raise_for_status()
        batch = resp.json().get("features", [])
        if not batch:
            break
        features.extend(f["attributes"] for f in batch)
        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.3)
    return features


def _guilford_fetch_parcel_addresses(reids: list[str]) -> dict[str, dict]:
    """Batch-fetch situs address + owner for a list of REIDs."""
    by_reid: dict[str, dict] = {}
    chunk_size = 150
    for i in range(0, len(reids), chunk_size):
        chunk = reids[i:i + chunk_size]
        where = "REID IN (" + ",".join(f"'{r}'" for r in chunk) + ")"
        params = {
            "where": where,
            "outFields": "REID,PHYADDR_STR_NUM,PHYADDR_DIR_PFX,PHYADDR_STR,"
                         "PHYADDR_STR_TYPE,PHYADDR_STR_SFX,PHYADDR_CITY,"
                         "PHYADDR_ZIP,PROPERTY_OWNER",
            "f": "json",
            "resultRecordCount": chunk_size,
        }
        resp = requests.get(GUILFORD_PARCEL_URL, params=params, timeout=REQUEST_TIMEOUT,
                             headers=_HEADERS)
        resp.raise_for_status()
        for f in resp.json().get("features", []):
            a = f["attributes"]
            reid = a.get("REID")
            if reid:
                by_reid[reid] = a
        time.sleep(0.3)
    return by_reid


def fetch_guilford_records() -> list[NoticeData]:
    """Fetch Guilford's full unpaid delinquent list with situs addresses."""
    delq_rows = _guilford_fetch_delinquent()
    if not delq_rows:
        logger.warning("Guilford: no delinquent records returned")
        return []

    by_parcel: dict[str, dict] = {}
    for row in delq_rows:
        reid = row.get("PARCEL_NUM")
        if not reid:
            continue
        entry = by_parcel.setdefault(reid, {
            "years": set(), "amount": 0.0, "owner": row.get("OWNER_NAME") or "",
            "taxable_value": 0.0,
        })
        if row.get("TAX_YEAR"):
            entry["years"].add(row["TAX_YEAR"])
        entry["amount"] += _to_float(row.get("TOTAL_DUE_AMOUNT"))
        entry["taxable_value"] = max(
            entry["taxable_value"], _to_float(row.get("ABSTRACT_TAXABLE_VALUE"))
        )

    parcel_info = _guilford_fetch_parcel_addresses(list(by_parcel.keys()))

    notices: list[NoticeData] = []
    today = date.today().isoformat()
    for reid, entry in by_parcel.items():
        p = parcel_info.get(reid)
        if not p:
            continue
        street_parts = [
            str(p.get("PHYADDR_STR_NUM") or "").strip(),
            str(p.get("PHYADDR_DIR_PFX") or "").strip(),
            str(p.get("PHYADDR_STR") or "").strip(),
            str(p.get("PHYADDR_STR_TYPE") or "").strip(),
            str(p.get("PHYADDR_STR_SFX") or "").strip(),
        ]
        address = " ".join(x for x in street_parts if x).strip()
        if not address:
            continue
        owner_name = p.get("PROPERTY_OWNER") or entry["owner"]

        n = NoticeData(
            date_added=today,
            address=address,
            city=str(p.get("PHYADDR_CITY") or "").strip().title(),
            state="NC",
            zip=str(p.get("PHYADDR_ZIP") or "").strip(),
            owner_name=owner_name,
            notice_type="tax_delinquent",
            county="Guilford",
            source_url=GUILFORD_DELQ_URL,
            parcel_id=reid,
            tax_delinquent_amount=str(round(entry["amount"], 2)),
            tax_delinquent_years=str(len(entry["years"]) or 1),
            tax_owner_name=owner_name,
            taxable_value=str(round(entry["taxable_value"], 2)) if entry["taxable_value"] else "",
        )
        notices.append(n)

    logger.info("Guilford: fetched %d delinquent properties", len(notices))
    return notices


# ── Orange County ────────────────────────────────────────────────────────
# Live fetch — same annual bulk XLSX + ArcGIS parcel join as
# nc_tax_enricher.py's Orange path (reuses its verified downloader). The
# bulk file has no per-year breakdown (documented limitation already noted
# in nc_tax_enricher.py), so tax_delinquent_years is left blank here rather
# than guessed — Orange records can't be verified against the 2+-years
# criterion from this source alone.

ORANGE_PARCEL_URL = (
    "https://gis.orangecountync.gov/arcgis/rest/services/"
    "WebParcelService/MapServer/0/query"
)


def _orange_fetch_parcels(pins: list[str]) -> dict[str, dict]:
    by_pin: dict[str, dict] = {}
    chunk_size = 150
    for i in range(0, len(pins), chunk_size):
        chunk = pins[i:i + chunk_size]
        where = "PIN IN (" + ",".join(f"'{p}'" for p in chunk) + ")"
        params = {
            "where": where,
            "outFields": "PIN,ADDRESS1,CITY,STATE,ZIPCODE,VALUATION,OWNER1",
            "f": "json",
            "resultRecordCount": chunk_size,
        }
        resp = requests.get(ORANGE_PARCEL_URL, params=params, timeout=REQUEST_TIMEOUT,
                             headers=_HEADERS)
        resp.raise_for_status()
        for f in resp.json().get("features", []):
            a = f["attributes"]
            pin = a.get("PIN")
            if pin:
                by_pin[pin] = a
        time.sleep(0.3)
    return by_pin


def fetch_orange_records() -> list[NoticeData]:
    """Fetch Orange's annual delinquent list joined against the parcel layer."""
    from nc_tax_enricher import _orange_download  # reuse verified downloader/cache

    path = _orange_download()
    if not path:
        logger.warning("Orange: bulk file unavailable")
        return []

    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip().upper() if h else "" for h in next(rows)]
    col = {name: i for i, name in enumerate(header)}

    required = {"NAME1", "PARCEL #", "BASE TAX AMT"}
    if not required.issubset(col):
        raise ValueError(f"Orange file missing expected columns: {required - set(col)}")

    delq: dict[str, dict] = {}
    for row in rows:
        pin = str(row[col["PARCEL #"]] or "").strip()
        if not pin:
            continue
        delq[pin] = {
            "owner": row[col["NAME1"]] or "",
            "amount": _to_float(row[col["BASE TAX AMT"]]),
        }

    parcel_info = _orange_fetch_parcels(list(delq.keys()))

    notices: list[NoticeData] = []
    today = date.today().isoformat()
    for pin, entry in delq.items():
        p = parcel_info.get(pin)
        if not p:
            continue
        address = str(p.get("ADDRESS1") or "").strip()
        if not address:
            continue
        owner_name = p.get("OWNER1") or entry["owner"]
        value = _to_float(p.get("VALUATION"))

        n = NoticeData(
            date_added=today,
            address=address,
            city=str(p.get("CITY") or "").strip().title(),
            state="NC",
            zip=str(p.get("ZIPCODE") or "").strip(),
            owner_name=owner_name,
            notice_type="tax_delinquent",
            county="Orange",
            source_url="https://www.orangecountync.gov",
            parcel_id=pin,
            tax_delinquent_amount=str(round(entry["amount"], 2)),
            tax_delinquent_years="",  # annual snapshot has no per-year breakdown
            tax_owner_name=owner_name,
            taxable_value=str(round(value, 2)) if value else "",
        )
        notices.append(n)

    logger.info("Orange: fetched %d delinquent properties", len(notices))
    return notices


# ── Durham County ────────────────────────────────────────────────────────
# Live fetch — same Spatialest "Bill PWA" endpoint as nc_tax_enricher.py's
# Durham path. Confirmed live 2026-08-18: the delinquent-list response has
# no taxable/assessed-value field, and no second endpoint for one was found
# (tried /getData.php with several plausible qtype values — all returned
# empty). taxable_value is left blank rather than guessed; see module
# docstring for what that means for the screen.

DURHAM_API_URL = "https://property.spatialest.com/nc/durham-tax/data/getData.php"
_DURHAM_ADDR_RE = re.compile(r"^(\d+)\s+(.+?)\s+DURHAM\s+NC\s+(\d{5})", re.IGNORECASE)


def fetch_durham_records() -> list[NoticeData]:
    """Fetch Durham's full delinquent list (one stateless call, ~3000 bills)."""
    resp = requests.post(
        DURHAM_API_URL,
        data={"qtype": "delinquint_list", "search": ""},  # vendor's own typo
        timeout=REQUEST_TIMEOUT,
        headers=_HEADERS,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])

    by_parcel: dict[str, dict] = {}
    for row in results:
        parcel = row.get("Parcel") or row.get("PARID")
        if not parcel:
            continue
        addr_raw = (row.get("AssetDescription") or "").strip()
        m = _DURHAM_ADDR_RE.match(addr_raw)
        entry = by_parcel.setdefault(parcel, {
            "years": set(), "amount": 0.0, "owner": row.get("NewOwner") or "",
            "house_number": m.group(1) if m else "", "street": m.group(2).strip() if m else "",
            "zip": m.group(3) if m else "",
        })
        entry["amount"] += _to_float(row.get("BalanceNum") or row.get("TotAdvBalanceDue"))
        bill = row.get("Bill", "")
        parts = bill.split("-")
        if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) == 4:
            entry["years"].add(parts[1])

    notices: list[NoticeData] = []
    today = date.today().isoformat()
    for parcel, entry in by_parcel.items():
        if not entry["house_number"] or not entry["street"]:
            continue
        address = f"{entry['house_number']} {entry['street']}"
        n = NoticeData(
            date_added=today,
            address=address,
            city="Durham",
            state="NC",
            zip=entry["zip"],
            owner_name=entry["owner"],
            notice_type="tax_delinquent",
            county="Durham",
            source_url=DURHAM_API_URL,
            parcel_id=parcel,
            tax_delinquent_amount=str(round(entry["amount"], 2)),
            tax_delinquent_years=str(len(entry["years"]) or 1),
            tax_owner_name=entry["owner"],
            taxable_value="",  # not available from this source — see module docstring
        )
        notices.append(n)

    logger.info("Durham: fetched %d delinquent properties", len(notices))
    return notices


# ── Financial screen ─────────────────────────────────────────────────────


def apply_financial_screen(notices: list[NoticeData]) -> tuple[list[NoticeData], _ScreenStats]:
    """Keep only records with >=2 years unpaid, >=$2,500 owed, >=$100k taxable value.

    A record with a blank/unknown years or taxable_value field (Durham has
    no value source, Orange has no year-count source) fails closed rather
    than being assumed to pass.
    """
    stats = _ScreenStats(total=len(notices))
    passed: list[NoticeData] = []

    for n in notices:
        years = n.tax_delinquent_years.strip()
        if not years or int(years) < SCREEN_MIN_YEARS_UNPAID:
            stats.excluded_years += 1
            continue

        amount = _to_float(n.tax_delinquent_amount)
        if amount < SCREEN_MIN_TOTAL_DUE:
            stats.excluded_amount += 1
            continue

        if not n.taxable_value.strip():
            stats.excluded_missing_value += 1
            continue
        value = _to_float(n.taxable_value)
        if value < SCREEN_MIN_TAXABLE_VALUE:
            stats.excluded_value += 1
            continue

        passed.append(n)

    stats.passed = len(passed)
    logger.info(
        "Financial screen: %d/%d passed (excluded: %d <%dyr, %d <$%.0f owed, "
        "%d <$%.0f value, %d missing value)",
        stats.passed, stats.total, stats.excluded_years, SCREEN_MIN_YEARS_UNPAID,
        stats.excluded_amount, SCREEN_MIN_TOTAL_DUE,
        stats.excluded_value, SCREEN_MIN_TAXABLE_VALUE, stats.excluded_missing_value,
    )
    return passed, stats


# ── Deceased-owner canvas ────────────────────────────────────────────────


def canvas_deceased_owners(notices: list[NoticeData], api_key: str) -> None:
    """Flag deceased owners in-place via obituary search.

    Lean mode: confirms deceased + date of death + obituary URL only — no
    heir/executor identification or address lookup (skip_heir_verification /
    skip_dm_address / skip_ancestry all True).
    """
    if not notices:
        return
    from obituary_enricher import enrich_obituary_data

    enrich_obituary_data(
        notices,
        api_key,
        skip_heir_verification=True,
        skip_dm_address=True,
        skip_ancestry=True,
    )
    confirmed = sum(1 for n in notices if n.owner_deceased == "yes")
    logger.info("Deceased-owner canvas: %d/%d confirmed deceased", confirmed, len(notices))


# ── Output ────────────────────────────────────────────────────────────────

OUTPUT_COLUMNS = [
    "county", "owner_name", "address", "city", "state", "zip", "parcel_id",
    "tax_delinquent_years", "tax_delinquent_amount", "taxable_value",
    "owner_deceased", "date_of_death", "obituary_url", "source_url",
]


def write_output_csv(notices: list[NoticeData], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for n in notices:
            writer.writerow({col: getattr(n, col, "") for col in OUTPUT_COLUMNS})
    logger.info("Wrote %d records to %s", len(notices), path)


# ── Orchestration ────────────────────────────────────────────────────────


def run(
    county: str,
    input_path: Path | None,
    output_path: Path,
    api_key: str = "",
    skip_obituary: bool = False,
) -> list[NoticeData]:
    county = county.strip().lower()
    if county not in SUPPORTED_COUNTIES:
        raise ValueError(
            f"Unsupported county '{county}' — must be one of {sorted(SUPPORTED_COUNTIES)}"
        )

    if county == "wake":
        if input_path is None:
            from nc_tax_enricher import _wake_download
            fetched = _wake_download()
            if not fetched:
                raise RuntimeError("No --input file given and Wake auto-download failed")
            input_path = fetched
        notices = parse_wake_bulk_file(input_path)
    elif county == "guilford":
        notices = fetch_guilford_records()
    elif county == "orange":
        notices = fetch_orange_records()
    elif county == "durham":
        notices = fetch_durham_records()
    else:  # pragma: no cover — guarded above
        notices = []

    passed, _stats = apply_financial_screen(notices)

    if not skip_obituary:
        if api_key:
            canvas_deceased_owners(passed, api_key)
        else:
            logger.warning("No Anthropic API key configured — skipping deceased-owner canvas")

    write_output_csv(passed, output_path)
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Screen raw NC tax-delinquent data for deceased owners + deal criteria "
                    "(2+ years unpaid, $2,500+ owed, $100k+ taxable value)"
    )
    parser.add_argument("--county", required=True, choices=sorted(SUPPORTED_COUNTIES),
                        help="Which county's delinquent data to screen")
    parser.add_argument("--input", type=Path, default=None,
                        help="Path to the raw county export (required for --county wake "
                             "unless you want today's file auto-downloaded; ignored for "
                             "guilford/orange/durham, which are always fetched live)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output CSV path (default: output/tax_delinquent_screen_"
                             "{county}_{date}.csv)")
    parser.add_argument("--skip-obituary", action="store_true",
                        help="Skip the deceased-owner obituary canvas (financial screen only)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    output_path = args.output or (
        config.OUTPUT_DIR
        / f"tax_delinquent_screen_{args.county}_{date.today().isoformat()}.csv"
    )

    run(
        county=args.county,
        input_path=args.input,
        output_path=output_path,
        api_key=config.ANTHROPIC_API_KEY,
        skip_obituary=args.skip_obituary,
    )


if __name__ == "__main__":
    main()
