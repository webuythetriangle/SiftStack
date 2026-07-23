"""Configuration for SiftStack — full-stack REI operations platform."""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = PROJECT_ROOT / "last_run.json"
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"
NC_SEEN_IDS_FILE = PROJECT_ROOT / "nc_seen_ids.json"
ECOURTS_SEEN_IDS_FILE = PROJECT_ROOT / "ecourts_seen_ids.json"  # keyed by case number (e.g. "26SP123"), not URL
SEEN_IDS_PRUNE_DAYS = 90
# Cross-run property-level lead registry (see property_registry.py). Keyed by
# address+zip+notice_type+county so a re-published/amended notice for a
# property already scraped gets merged into the existing lead instead of
# appearing as a second row. Longer prune window than seen_ids since
# foreclosure/tax-sale/probate processes can span months between notices.
SEEN_PROPERTIES_FILE = PROJECT_ROOT / "seen_properties.json"
SEEN_PROPERTIES_PRUNE_DAYS = 180
# Notices that exhausted all CAPTCHA retries during scraping.
# Persisted so the next run's summary can surface them instead of
# silently dropping — and a future retry pass can prioritize them.
CAPTCHA_FAILED_IDS_FILE = PROJECT_ROOT / "captcha_failed_ids.json"
CAPTCHA_FAILED_PRUNE_DAYS = 14
COOKIES_FILE = PROJECT_ROOT / "cookies.json"
DROPBOX_STATE_FILE = PROJECT_ROOT / "dropbox_state.json"
PHOTO_STATE_FILE = PROJECT_ROOT / "photo_state.json"
# Downloaded county bulk tax files (Wake daily XLSX, Orange annual XLSX) —
# cached to disk so a run doesn't re-download an unchanged file. See
# nc_tax_enricher.py.
NC_TAX_CACHE_DIR = PROJECT_ROOT / "nc_tax_cache"

# ── Dropbox Watcher ────────────────────────────────────────────────────
DROPBOX_POLL_INTERVAL = int(os.getenv("DROPBOX_POLL_INTERVAL", "900"))  # seconds (default 15 min)
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "")  # root folder path in Dropbox, e.g. "/TN Public Notice"
DROPBOX_STORAGE_WARN_PERCENT = 80  # warn when storage usage exceeds this %

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
NC_TAX_CACHE_DIR.mkdir(exist_ok=True)

# ── Credentials ────────────────────────────────────────────────────────
TNPN_EMAIL = os.getenv("TNPN_EMAIL", "")
TNPN_PASSWORD = os.getenv("TNPN_PASSWORD", "")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")  # 2Captcha API key
SCRAPFLY_API_KEY = os.getenv("SCRAPFLY_API_KEY", "")  # Scrapfly anti-bot scraping API (see scrapfly_solver.py — not wired in by default)
SCRAPFLY_PROXY_POOL = os.getenv("SCRAPFLY_PROXY_POOL", "public_residential_pool")
# Tags attached to every Scrapfly request for dashboard-side filtering/analytics
# (Scrapfly's "Monitoring" tab groups usage/cost by tag). Comma-separated env override.
SCRAPFLY_TAGS = [t.strip() for t in os.getenv("SCRAPFLY_TAGS", "siftstack,ecourts-scraper").split(",") if t.strip()]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Haiku for LLM parsing
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")        # Smarty address standardization
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
OPENWEBNINJA_API_KEY = os.getenv("OPENWEBNINJA_API_KEY", "")  # Zillow property enrichment
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")              # Serper.dev Google Search API
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")        # Firecrawl JS-rendered scraping
TRACERFY_API_KEY = os.getenv("TRACERFY_API_KEY", "")          # Tracerfy skip tracing
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")            # Trestle phone validation
DATASIFT_EMAIL = os.getenv("DATASIFT_EMAIL", "")              # DataSift.ai login
DATASIFT_PASSWORD = os.getenv("DATASIFT_PASSWORD", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")        # Slack/Discord webhook
ANCESTRY_EMAIL = os.getenv("ANCESTRY_EMAIL", "")              # Ancestry.com login
ANCESTRY_PASSWORD = os.getenv("ANCESTRY_PASSWORD", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")            # Dropbox OAuth2 app key
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")
NARRPR_EMAIL = os.getenv("NARRPR_EMAIL", "")                  # narrpr.com (RPR) login
NARRPR_PASSWORD = os.getenv("NARRPR_PASSWORD", "")

# ── LLM Backend ──────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")           # "anthropic", "ollama", or "openrouter"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")  # Anthropic model name
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")        # Local Ollama model
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")       # OpenRouter API key
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# ── Site URLs ──────────────────────────────────────────────────────────
BASE_URL = "https://www.tnpublicnotice.com"
LOGIN_URL = f"{BASE_URL}/authenticate.aspx"
SMART_SEARCH_URL = f"{BASE_URL}/Smartsearch/Default.aspx"

# ── ASP.NET Selectors ─────────────────────────────────────────────────
# Login form
SEL_LOGIN_EMAIL = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtEmailAddress"
SEL_LOGIN_PASSWORD = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtPassword"
SEL_LOGIN_SUBMIT = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_btnAuth"

# Smart Search dashboard
SEL_SAVED_SEARCHES_DROPDOWN = "#ctl00_ContentPlaceHolder1_as1_ddlSavedSearches"
SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'

# Search results (authenticated grid)
SEL_RESULTS_GRID = "#ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
SEL_VIEW_BUTTON_PATTERN = "input[name$='btnView']"
SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
SEL_PAGE_INFO = "td:has-text('Page ')"

# Notice detail page
SEL_CAPTCHA_IFRAME = "iframe[src*='recaptcha']"
SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"
RECAPTCHA_SITEKEY = "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"

# ── Rate Limiting ──────────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0  # seconds between requests
REQUEST_DELAY_MAX = 3.0
MAX_RETRIES = 3
RESULTS_PER_PAGE = 50  # max the site allows

# ncnotices_scraper: if the results-page DOM goes stale mid-scan (e.g. a
# transient network stall corrupts browser state), restart the whole
# county/keyword search from scratch this many times before giving up.
# seen_ids lets a restart skip notices already collected instead of redoing them.
NC_SEARCH_RESTART_LIMIT = 3

# ── Image Processing ───────────────────────────────────────────────────
BLUR_THRESHOLD = int(os.getenv("BLUR_THRESHOLD", "100"))   # Laplacian variance; below = rejected as blurry
TESSERACT_PSM_PDF = 3    # fully automatic — best for PDF tax sale tables
TESSERACT_PSM_PHOTO = 4  # assume single column of variable-size text — best for terminal screen photos

# ── Notice Types ───────────────────────────────────────────────────────
NOTICE_TYPES = ["foreclosure", "probate"]


@dataclass
class SavedSearch:
    """Represents a saved search on tnpublicnotice.com."""
    county: str
    notice_type: str  # One of NOTICE_TYPES
    saved_search_name: str  # Exact name in the Saved Searches dropdown


# ── Saved Searches ─────────────────────────────────────────────────────
# These names must match exactly what appears in the dropdown on the site.
SAVED_SEARCHES: list[SavedSearch] = [
    SavedSearch("Knox", "foreclosure", "Foreclosure V2 Knox"),
    SavedSearch("Blount", "foreclosure", "Foreclosure V2 Blount"),
]

# ── ncnotices.com (North Carolina Press Association public notices) ────
# Second scrape source, added for NC market expansion (Wake, Durham, Orange,
# Guilford, Mecklenburg counties). Built on the same WebStrides ASP.NET
# platform as tnpublicnotice.com but with a keyword-search model instead of
# named saved searches, and Cloudflare Turnstile (not Google reCAPTCHA) gating
# each notice detail page. No login is required for basic search.
NC_BASE_URL = "https://www.ncnotices.com"
NC_SEARCH_URL = f"{NC_BASE_URL}/Search.aspx"

# Search form (Search.aspx) — county/date panels are collapsed by default
# and must be opened (click the toggle div) before their inputs are usable.
NC_SEL_SEARCH_KEYWORD = "#ctl00_ContentPlaceHolder1_as1_txtSearch"
# "All Words" (rdoType_0, default) / "Any Words" (rdoType_1) / "Exact Phrase" (rdoType_2).
NC_SEL_MATCH_ANY_WORDS_LABEL = "label[for='ctl00_ContentPlaceHolder1_as1_rdoType_1']"
NC_SEL_COUNTY_TOGGLE = "#ctl00_ContentPlaceHolder1_as1_divCounty"
NC_SEL_COUNTY_LIST = "#ctl00_ContentPlaceHolder1_as1_lstCounty"
NC_SEL_DATE_TOGGLE = "#ctl00_ContentPlaceHolder1_as1_divDateRange"
NC_SEL_LAST_NUM_DAYS_RADIO = "#ctl00_ContentPlaceHolder1_as1_rbLastNumDays"
NC_SEL_LAST_NUM_DAYS_INPUT = "#ctl00_ContentPlaceHolder1_as1_txtLastNumDays"
# Custom From/To range — site note: "past 12 months are available in the
# current search... use Archive Search for notices older than 12 months"
# (/Archive/ArchiveSearch.aspx, not implemented here — out of scope until
# something actually needs data older than a year).
NC_SEL_DATE_RANGE_RADIO = "#ctl00_ContentPlaceHolder1_as1_rbRange"
NC_SEL_DATE_FROM_INPUT = "#ctl00_ContentPlaceHolder1_as1_txtDateFrom"
NC_SEL_DATE_TO_INPUT = "#ctl00_ContentPlaceHolder1_as1_txtDateTo"
NC_SEL_SEARCH_SUBMIT = "#ctl00_ContentPlaceHolder1_as1_btnGo"

# Search results grid
NC_SEL_VIEW_BUTTON_PATTERN = "input[id$='btnView2']"
NC_SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
NC_SEL_PAGE_INFO = "td:has-text('Page ')"
NC_SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'

# Notice detail page — Cloudflare Turnstile gate (not Google reCAPTCHA)
NC_TURNSTILE_SITEKEY = "0x4AAAAAADs-29tdUBxeI6cO"
NC_SEL_TURNSTILE_RESPONSE = "input[name='cf-turnstile-response']"
NC_SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"


@dataclass
class NCSearch:
    """Represents a keyword search on ncnotices.com (county + notice type + keyword)."""
    county: str
    notice_type: str  # Only "foreclosure" is supported today — see NC_SAVED_SEARCHES note
    keyword: str
    # Note: the "foreclosure" keyword search returns both power-of-sale trustee
    # foreclosures and judicial tax foreclosures mixed together. ncnotices_scraper
    # reclassifies the tax ones to notice_type="tax_foreclosure" per-notice after
    # fetching (see foreclosure_filter.is_tax_foreclosure) — no separate search
    # needed, so tax foreclosures ride along automatically wherever this search runs.


# ── NC Saved Searches ───────────────────────────────────────────────────
# ncnotices.com is a general legal-notice aggregator with a free-text keyword
# search, not a fixed category taxonomy — there is no "Probate", "Tax Sale",
# "Tax Delinquent", "Eviction", or "Code Violation" search category here.
# Only foreclosure sale notices (published under NCGS 45-21.17) are reliably
# findable via keyword search. The other 5 notice types live on different NC
# systems entirely (NC eCourts/Odyssey for probate + eviction, wake.gov bulk
# files for tax delinquency, county Sheriff's office for tax sale, and
# city/county code-enforcement portals for code violations) and need their
# own separate integrations — not in scope here.
# Keyword is "foreclosure trustee" (not just "foreclosure") searched with Any
# Words matching — confirmed live (Durham, 2026-07-23) that some genuine
# power-of-sale and HOA-lien trustee sale notices are titled "NOTICE OF SALE"
# or "NOTICE OF TRUSTEE'S SALE OF REAL PROPERTY" and never use the word
# "foreclosure" anywhere in the text, so a "foreclosure"-only search silently
# missed them. Every genuine trustee sale necessarily mentions "trustee"
# (that's who executes it), so OR-ing the two catches both phrasings.
# ncnotices_scraper._run_nc_search_once selects the "Any Words" radio
# automatically whenever a keyword contains more than one word.
NC_SAVED_SEARCHES: list[NCSearch] = [
    NCSearch("Wake", "foreclosure", "foreclosure trustee"),
    NCSearch("Durham", "foreclosure", "foreclosure trustee"),
    NCSearch("Orange", "foreclosure", "foreclosure trustee"),
    NCSearch("Guilford", "foreclosure", "foreclosure trustee"),
    NCSearch("Mecklenburg", "foreclosure", "foreclosure trustee"),
]

# ── NC eCourts Portal (Tyler Technologies Odyssey — statewide, all 100 NC
# counties as of the Oct 13, 2025 full rollout) ─────────────────────────
# A power-of-sale foreclosure under a deed of trust is filed as a Special
# Proceeding (NCGS Chapter 45, Article 2A) with the Clerk of Superior Court —
# the trustee/substitute trustee files a "Notice of Hearing" here BEFORE any
# sale notice is published in the newspaper (ncnotices_scraper.py's source,
# which only sees the case weeks later at publication). Catching the SP
# filing/Notice of Hearing at eCourts is materially first-to-market.
#
# Confirmed live (2026-07-19): redirects from the legacy
# www3.nccourts.org/onlineservices/menu.sp URL. Protected by AWS WAF Bot
# Control with a CAPTCHA "Human Verification" interstitial — NOT Google
# reCAPTCHA (tnpublicnotice.com) or Cloudflare Turnstile (ncnotices.com).
# 2Captcha has no turnkey AWS WAF token type, which is why this source uses
# Scrapfly's ASP (anti-scraping-protection) bypass instead — see
# ecourts_scraper.py. Scrapfly's ASP shield did not clear on the first
# live attempt against this portal (ERR::ASP::SHIELD_PROTECTION_FAILED) —
# per Scrapfly's own docs this is expected and warrants retrying; treat
# occasional shield failures as a retryable condition, not a hard failure.
ECOURTS_BASE_URL = "https://portal-nc.tylertech.cloud/Portal"
ECOURTS_SMART_SEARCH_URL = f"{ECOURTS_BASE_URL}/Home/Dashboard/29"
ECOURTS_CASE_CATEGORY = "Special Proceedings"

# Same 5-county NC expansion market as NC_SAVED_SEARCHES (ncnotices_scraper.py).
ECOURTS_TARGET_COUNTIES: list[str] = [
    "Wake", "Durham", "Orange", "Guilford", "Mecklenburg",
]

# ── Entity Detection ──────────────────────────────────────────────────
# Business entity patterns — shared across obituary_enricher, tax_enricher,
# and enrichment_pipeline for entity filtering.
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|"
    r"PARTNERSHIP|ASSOCIATION|ASSOC|BANK|CREDIT UNION|CHURCH|MINISTRIES|"
    r"HOUSING|AUTHORITY|DEVELOPMENT|ENTERPRISES|PROPERTIES|INVESTMENTS|"
    r"GROUP|HOLDINGS|MANAGEMENT|SERVICES|FOUNDATION|ORGANIZATION)\b",
    re.IGNORECASE,
)

# Trust/estate patterns — personal trusts are NOT business entities
TRUST_NAME_RE = re.compile(
    r"^(?:THE\s+)?([\w]+(?:\s+[\w.]+)+?)\s+(?:REVOCABLE\s+)?(?:LIVING\s+)?TRUST\b",
    re.IGNORECASE,
)
ESTATE_OF_RE = re.compile(
    r"^(?:THE\s+)?ESTATE\s+OF\s+([\w]+(?:\s+[\w.]+)+?)(?:\s*,|\s*$)",
    re.IGNORECASE,
)

_config_logger = logging.getLogger(__name__)


# ── State File Utilities ─────────────────────────────────────────────


def save_state(path: Path, data: dict) -> None:
    """Write JSON state to disk atomically (write tmp → rename).

    Creates a .bak copy of the previous file before overwriting.
    """
    # Back up current file
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass  # Best-effort backup

    # Atomic write: tmp → rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _config_logger.warning("Failed to read %s: %s", candidate, e)
    return {}
