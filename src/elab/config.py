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
