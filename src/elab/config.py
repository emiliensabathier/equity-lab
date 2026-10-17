"""Statement lines the engine reads, split by what a missing line means.

A core line cannot be assumed away: a zero for pretax income, tax, capex or D&A would inflate
the value, so the company is refused. A defaultable line is one where "not reported" plausibly
means "none" (no minorities, no leases, no property disposals); it is treated as zero and
surfaced in the page's limits instead of being hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

CORE_LINES: dict[str, tuple[str, ...]] = {
    "income": ("Total Revenue", "EBIT", "Pretax Income", "Tax Provision"),
    "cashflow": ("Capital Expenditure", "Depreciation And Amortization"),
    "balance": (
        "Total Debt",
        "Cash Cash Equivalents And Short Term Investments",
        "Ordinary Shares Number",
    ),
}

DEFAULTABLE_LINES: dict[str, tuple[str, ...]] = {
    "income": ("Interest Expense",),
    "cashflow": ("Sale Of PPE",),
    "balance": (
        "Minority Interest",
        "Capital Lease Obligations",
        "Current Capital Lease Obligation",
        "Inventory",
        "Accounts Receivable",
        "Accounts Payable",
    ),
}

# Defaultable lines that take the most recent earlier reported value when a reported year is
# blank, instead of zero: a blank latest-year interest expense read as zero shows a 0% cost of
# debt, and a blank lease line read as zero counts the leases as financial debt.
CARRIED_LINES = frozenset({
    "Interest Expense", "Capital Lease Obligations", "Current Capital Lease Obligation",
})

US_EXCHANGES = frozenset({"NMS", "NYQ", "NGM", "NCM", "ASE", "BTS", "PCX"})

# Benchmark per listing suffix ("" = US). Each symbol was checked to return weekly closes from
# yfinance; OBX.OL returned none and is left to the fallback.
INDEX_BY_SUFFIX: dict[str, str] = {
    "": "^GSPC",
    ".PA": "^FCHI",
    ".DE": "^GDAXI",
    ".L": "^FTSE",
    ".SW": "^SSMI",
    ".AS": "^AEX",
    ".MC": "^IBEX",
    ".MI": "FTSEMIB.MI",
    ".ST": "^OMX",
    ".BR": "^BFX",
    ".CO": "^OMXC25",
    ".HE": "^OMXH25",
    ".LS": "PSI20.LS",
    ".VI": "^ATX",
    ".IR": "^ISEQ",
}
FALLBACK_INDEX = "^STOXX"

# Quote currency of each benchmark, checked against yfinance's metadata on 2026-10-17. The beta
# regresses the stock as quoted against its index in that same trading currency, with no
# exchange rate in the regression; an index quoted in another currency (the ^STOXX fallback for
# a non-euro listing, a London line quoted in USD) is converted to the stock's currency first.
INDEX_CURRENCY: dict[str, str] = {
    "^GSPC": "USD",
    "^FCHI": "EUR",
    "^GDAXI": "EUR",
    "^FTSE": "GBP",
    "^SSMI": "CHF",
    "^AEX": "EUR",
    "^IBEX": "EUR",
    "FTSEMIB.MI": "EUR",
    "^OMX": "SEK",
    "^BFX": "EUR",
    "^OMXC25": "DKK",
    "^OMXH25": "EUR",
    "PSI20.LS": "EUR",
    "^ATX": "EUR",
    "^ISEQ": "EUR",
    "^STOXX": "EUR",
}

# How long after the latest fiscal year-end a price may be compared with its statements. vlab's
# default is one year; a site priced every day meets companies between their year-end's first
# anniversary and the filing of the next annual report (a US 10-K is due within 60-90 days, the
# EU allows four months, and yfinance takes some days more). Apple, priced on 2026-10-16 against
# a 2025-09-30 year-end, sat at 1.03 years. Beyond 18 months the statements are stale.
MAX_VALUATION_LAG_YEARS = 1.5

EXCLUDED_SECTORS = frozenset({"Financial Services", "Real Estate"})

TICKER_PATTERN = r"^[A-Z0-9][A-Z0-9.\-]{0,14}$"

# 10-year government bond yields by reporting currency (EUR = German Bund), the risk-free rate
# each company is discounted at. Source: tradingeconomics.com/bonds, closes of 2026-10-16;
# the USD figure matches the US Treasury daily par yield curve (5.09%) for the same date.
# Assumptions, not measurements: the page states the rate and its date in the limits.
RISK_FREE: dict[str, float] = {
    "USD": 0.0509,
    "EUR": 0.0342,
    "GBP": 0.0538,
    "CHF": 0.0055,
    "SEK": 0.0317,
    "DKK": 0.0322,
    "NOK": 0.0456,
}
RISK_FREE_DATE = "2026-10-16"


@dataclass(frozen=True)
class ConstituentSource:
    """A Wikipedia page listing an index's members, and the column holding their tickers."""

    index: str
    url: str
    ticker_column: str
    suffix: str  # Yahoo listing suffix appended when the page's tickers do not carry it


_WIKI = "https://en.wikipedia.org/wiki/"

# Columns checked against the live pages on 2026-10-17. The table is found by its ticker column,
# not by position, since Wikipedia reorders tables. Pages that already print the Yahoo suffix
# (CAC 40, DAX, AEX, IBEX, MIB, OMX) are not double-suffixed; FTSE 100 and SMI print bare codes.
CONSTITUENT_SOURCES: tuple[ConstituentSource, ...] = (
    ConstituentSource("S&P 500", _WIKI + "List_of_S%26P_500_companies", "Symbol", ""),
    ConstituentSource("CAC 40", _WIKI + "CAC_40", "Ticker", ".PA"),
    ConstituentSource("DAX", _WIKI + "DAX", "Ticker", ".DE"),
    ConstituentSource("FTSE 100", _WIKI + "FTSE_100_Index", "Ticker", ".L"),
    ConstituentSource("SMI", _WIKI + "Swiss_Market_Index", "Ticker", ".SW"),
    ConstituentSource("AEX", _WIKI + "AEX_index", "Ticker", ".AS"),
    ConstituentSource("IBEX 35", _WIKI + "IBEX_35", "Ticker", ".MC"),
    ConstituentSource("FTSE MIB", _WIKI + "FTSE_MIB", "Ticker", ".MI"),
    ConstituentSource("OMX Stockholm 30", _WIKI + "OMX_Stockholm_30", "Ticker", ".ST"),
)

PEER_TABLE_FILE = "peers.parquet"
PEER_REFRESH_DAYS = 7
MIN_PEERS = 4
MAX_PEERS = 8

# Analyses are re-fetched after a day; narratives never expire because their key is the content.
ANALYSIS_TTL = timedelta(hours=24)

# Per-client-IP budgets per sliding hour: only work that costs us something is counted
# (a cache miss that triggers a fresh analysis, a new narrative job), never cached reads.
COLD_ANALYSES_PER_HOUR = 10
NARRATIVE_JOBS_PER_HOUR = 5
RATE_WINDOW = timedelta(hours=1)
