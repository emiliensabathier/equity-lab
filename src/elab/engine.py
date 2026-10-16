"""One ticker in, the Analysis JSON out: the single contract the API, narrative and page read.

Each section is computed in isolation. A company whose price implies growth outside the
reverse DCF's bracket still has a balance sheet worth showing, so a DataError or
ValuationError in one section marks that section unavailable with its reason instead of
failing the page. Only two failures are whole-analysis: an unknown ticker (the API's 404) and
a stock price series that cannot be loaded, without which no number on the page means
anything.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from vlab.data.loader import (
    PriceFetcher,
    StatementFetcher,
    Statements,
    load_prices,
    load_statements,
)
from vlab.errors import DataError, ValuationError
from vlab.pipeline import CompanyResult, value_company

from elab.config import RISK_FREE, RISK_FREE_DATE
from elab.resolve import CompanyMeta, company_from_info
from elab.statements import fill_defaults, limit_sentence

# vlab is not written for concurrent runs (shared on-disk cache, BLAS threads); one at a time.
_LOCK = threading.Lock()

UNAVAILABLE_MULTIPLES = {"status": "unavailable", "reason": "peer table not built yet"}


@dataclass(frozen=True)
class Fetchers:
    """Every network access the engine makes, injectable so tests never touch the network."""

    statements: StatementFetcher
    prices: PriceFetcher
    info: Callable[[str], dict]


def _yahoo_info(ticker: str) -> dict:
    import yfinance as yf

    return dict(yf.Ticker(ticker).info)


def default_fetchers() -> Fetchers:
    """The live yfinance fetchers."""
    from vlab.data.loader import yfinance_fetcher, yfinance_price_fetcher

    return Fetchers(yfinance_fetcher, yfinance_price_fetcher, _yahoo_info)


def _clean(value: object) -> object:
    """Recursively replace NaN and infinities by None so the JSON is strict."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _grid(table: pd.DataFrame) -> dict:
    return {
        "rows": [float(row) for row in table.index],
        "cols": [float(col) for col in table.columns],
        "values": [[float(cell) for cell in row] for row in table.to_numpy()],
    }


def _dcf_section(result: CompanyResult, index: str) -> dict:
    return {
        "status": "ok",
        "value_per_share": result.value_per_share,
        "implied_growth": result.implied_growth,
        "implied_average_growth": result.implied_average_growth,
        "implied_average_growth_at_lower_wacc": {
            f"{step:.2f}": growth
            for step, growth in result.implied_average_growth_at_lower_wacc.items()
        },
        "normalized_growth": result.normalized_growth,
        "wacc": result.wacc,
        "beta": result.beta,
        "beta_index": index,
        "cost_of_equity": result.cost_of_equity,
        "cost_of_debt": result.cost_of_debt,
        "equity_weight": result.equity_weight,
        "debt_weight": result.debt_weight,
        "terminal_share": result.terminal_share,
        "exit_multiple": result.exit_multiple,
        "fiscal_years": list(result.drivers.fiscal_years),
        "sensitivity": _grid(result.sensitivity),
        "margin_sensitivity": _grid(result.margin_sensitivity),
    }


def _run_dcf(
    meta: CompanyMeta, statements: Statements, prices: pd.DataFrame,
    fetchers: Fetchers, cache_dir: Path,
) -> CompanyResult:
    ccy = meta.company.reporting_currency
    if ccy not in RISK_FREE:
        raise DataError(f"no risk-free rate configured for {ccy}")
    index = meta.market_index
    market = load_prices([index], cache_dir=cache_dir, fetcher=fetchers.prices)[index]
    # The filled statements are handed over through a fetcher with refresh=True, in a cache
    # directory of their own: the raw cache must keep the unfilled lines, or the next run would
    # find nothing to default and drop the matching limits.
    return value_company(
        meta.company, prices, market,
        cache_dir=cache_dir / "filled", refresh=True,
        statement_fetcher=lambda _ticker: statements,
        price_fetcher=fetchers.prices, risk_free=RISK_FREE[ccy],
    )


def _ratio(numerator: float, denominator: float) -> float | None:
    if not (math.isfinite(numerator) and math.isfinite(denominator)) or denominator == 0.0:
        return None
    return numerator / denominator


def _line(frame: pd.DataFrame, line: str, *, default: float | None = None) -> pd.Series:
    if line in frame.index:
        return frame.loc[line].astype(float)
    if default is None:
        raise DataError(f"statement line {line!r} is not reported")
    return pd.Series(default, index=frame.columns)


def _health_years(s: Statements) -> list[dict]:
    revenue = _line(s.income, "Total Revenue")
    ebit = _line(s.income, "EBIT")
    fcf = _line(s.cashflow, "Operating Cash Flow") + _line(s.cashflow, "Capital Expenditure")
    ebitda = ebit + _line(s.cashflow, "Depreciation And Amortization")
    net_debt = (
        _line(s.balance, "Total Debt")
        - _line(s.balance, "Capital Lease Obligations", default=0.0)
        - _line(s.balance, "Cash Cash Equivalents And Short Term Investments")
    )
    years = []
    for year in sorted(revenue.index):
        cover = float(ebitda.get(year, math.nan))
        years.append({
            "fiscal_year": str(pd.Timestamp(year).date()),
            "revenue": float(revenue[year]),
            "ebit_margin": _ratio(float(ebit.get(year, math.nan)), float(revenue[year])),
            "fcf": float(fcf.get(year, math.nan)),
            "net_debt_to_ebitda": (
                _ratio(float(net_debt.get(year, math.nan)), cover) if cover > 0.0 else None
            ),
        })
    return years


def _unavailable(exc: Exception) -> dict:
    return {"status": "unavailable", "reason": str(exc)}


def _limits(
    meta: CompanyMeta, filled: tuple[str, ...], income: pd.DataFrame | None
) -> list[str]:
    """Every assumption the page rests on; the drivers window only when statements loaded."""
    ccy = meta.company.reporting_currency
    rf_sentence = (
        f"Equity risk premium fixed at 5%; risk-free rate {RISK_FREE[ccy]:.2%} for {ccy} "
        f"(as of {RISK_FREE_DATE})."
        if ccy in RISK_FREE
        else f"No risk-free rate configured for {ccy}."
    )
    limits = [
        *(limit_sentence(line) for line in filled),
        rf_sentence,
        f"Beta regressed on weekly returns against {meta.market_index} over five years.",
    ]
    if income is not None and len(income.columns):
        years = sorted(pd.Timestamp(column).year for column in income.columns)
        limits.append(
            f"Normalized drivers are medians over the reported years ({years[0]}–{years[-1]})."
        )
    return limits


def _verdict(price: float, ccy: str, dcf: dict) -> str | None:
    if dcf["status"] != "ok":
        return None
    return (
        f"At {price:,.2f} {ccy}, the market prices {dcf['implied_average_growth']:.1%} "
        f"average revenue growth over five years; the company's normalized history is "
        f"{dcf['normalized_growth']:.1%}."
    )


def _header(ticker: str, meta: CompanyMeta) -> dict:
    return {
        "ticker": ticker,
        "name": meta.company.name,
        "exchange": meta.exchange,
        "sector": meta.sector,
        "industry": meta.industry,
        "reporting_currency": meta.company.reporting_currency,
        "trading_currency": meta.company.trading_currency,
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _sections(
    meta: CompanyMeta, prices: pd.DataFrame, fetchers: Fetchers, cache_dir: Path
) -> tuple[dict, dict, list[str]]:
    """The DCF and health sections plus the limits, each failure contained in its section."""
    ticker = meta.company.ticker
    raw = load_statements(ticker, cache_dir=cache_dir, fetcher=fetchers.statements)
    try:
        health = {"status": "ok", "years": _health_years(raw)}
    except (DataError, ValuationError) as exc:
        health = _unavailable(exc)
    filled: tuple[str, ...] = ()
    try:
        statements, filled = fill_defaults(raw)
        dcf = _dcf_section(_run_dcf(meta, statements, prices, fetchers, cache_dir),
                           meta.market_index)
    except (DataError, ValuationError) as exc:
        dcf = _unavailable(exc)
    return dcf, health, _limits(meta, filled, raw.income)


def _analyze(ticker: str, fetchers: Fetchers, cache_dir: Path) -> dict:
    meta = company_from_info(ticker, fetchers.info(ticker))
    header = _header(ticker, meta)
    if meta.excluded_reason is not None:
        return {**header, "price": None, "price_date": None, "coverage": "not_covered",
                "verdict": None, "limits": [meta.excluded_reason]}

    # A frame we cannot load is a whole-analysis failure; scaling builds a new frame.
    prices = load_prices([ticker], cache_dir=cache_dir, fetcher=fetchers.prices)
    prices = prices * meta.price_scale
    price = float(prices[ticker].iloc[-1])
    try:
        dcf, health, limits = _sections(meta, prices, fetchers, cache_dir)
    except DataError as exc:  # statements could not be loaded at all
        dcf = health = _unavailable(exc)
        limits = _limits(meta, (), None)
    full = dcf["status"] == "ok" and health["status"] == "ok"
    return {
        **header,
        "price": price,
        "price_date": str(prices.index[-1].date()),
        "coverage": "full" if full else "partial",
        "verdict": _verdict(price, meta.company.trading_currency, dcf),
        "dcf": dcf,
        "health": health,
        "multiples": dict(UNAVAILABLE_MULTIPLES),
        "limits": limits,
    }


def analyze(ticker: str, *, fetchers: Fetchers, cache_dir: Path) -> dict:
    """The Analysis JSON for one ticker; strict JSON (no NaN). Runs are serialized.

    ``UnknownTicker`` and a stock price that cannot be loaded propagate; every other data or
    model failure is reported inside the section it affects.
    """
    with _LOCK:
        return _clean(_analyze(ticker, fetchers, Path(cache_dir)))  # type: ignore[return-value]
