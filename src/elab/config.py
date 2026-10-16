"""Statement lines the engine reads, split by what a missing line means.

A core line cannot be assumed away: a zero for pretax income, tax, capex or D&A would inflate
the value, so the company is refused. A defaultable line is one where "not reported" plausibly
means "none" (no minorities, no leases, no property disposals); it is treated as zero and
surfaced in the page's limits instead of being hidden.
"""

from __future__ import annotations

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
