"""Hand-built statement fixtures; no network, no third-party data."""

from __future__ import annotations

import pandas as pd
from vlab.data.loader import Statements

YEARS = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])

_INCOME = {
    "Total Revenue": [1000.0, 940.0, 880.0, 820.0],
    "EBIT": [200.0, 185.0, 170.0, 150.0],
    "Pretax Income": [190.0, 175.0, 160.0, 140.0],
    "Tax Provision": [47.0, 44.0, 40.0, 35.0],
    "Interest Expense": [10.0, 10.0, 10.0, 10.0],
}
_CASHFLOW = {
    "Capital Expenditure": [-60.0, -55.0, -52.0, -48.0],
    "Sale Of PPE": [2.0, 1.0, 3.0, 1.0],
    "Depreciation And Amortization": [50.0, 47.0, 44.0, 41.0],
    "Operating Cash Flow": [230.0, 210.0, 195.0, 170.0],
}
_BALANCE = {
    "Total Debt": [300.0, 310.0, 320.0, 330.0],
    "Cash Cash Equivalents And Short Term Investments": [120.0, 110.0, 100.0, 90.0],
    "Minority Interest": [5.0, 5.0, 4.0, 4.0],
    "Ordinary Shares Number": [100.0, 100.0, 101.0, 102.0],
    "Capital Lease Obligations": [80.0, 78.0, 75.0, 70.0],
    "Current Capital Lease Obligation": [12.0, 11.0, 11.0, 10.0],
    "Inventory": [150.0, 142.0, 135.0, 128.0],
    "Accounts Receivable": [90.0, 85.0, 80.0, 75.0],
    "Accounts Payable": [70.0, 66.0, 62.0, 58.0],
}


def _frame(rows: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=YEARS).T


def make_statements(**overrides: pd.DataFrame | dict[str, object]) -> Statements:
    """Four annual columns, every line vlab reads plus Operating Cash Flow.

    Keyword overrides replace a whole field (income, cashflow, balance, info).
    """
    fields: dict[str, object] = {
        "income": _frame(_INCOME),
        "cashflow": _frame(_CASHFLOW),
        "balance": _frame(_BALANCE),
        "info": {"currency": "EUR"},
    }
    fields.update(overrides)
    return Statements(**fields)  # type: ignore[arg-type]
