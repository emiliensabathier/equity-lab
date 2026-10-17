"""One ticker in, the Analysis JSON out: the single contract the API, narrative and page read.

Each section is computed in isolation. A company whose price implies growth outside the
reverse DCF's bracket still has a balance sheet worth showing, so a DataError or
ValuationError in one section marks that section unavailable with its reason instead of
failing the page. Only two failures are whole-analysis: an unknown ticker (the API's 404) and
a stock price series that cannot be loaded, without which no number on the page means
anything.
"""

from __future__ import annotations

import logging
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
from vlab.dcf import MAX_VALUATION_LAG, Assumptions, growth_path
from vlab.errors import DataError, ValuationError
from vlab.fundamentals import drivers_from
from vlab.pipeline import DAYS_PER_YEAR, TERMINAL_GROWTH, CompanyResult, value_company
from vlab.reverse import GROWTH_BRACKET, GrowthOutOfRange

from elab.config import (
    INDEX_CURRENCY,
    MAX_VALUATION_LAG_YEARS,
    PEER_TABLE_FILE,
    RISK_FREE,
    RISK_FREE_DATE,
)
from elab.peers import peers_for
from elab.resolve import CompanyMeta, company_from_info
from elab.statements import (
    Carried,
    blank_years_sentence,
    carried_sentence,
    fill_blank_years,
    fill_defaults,
    limit_sentence,
)

log = logging.getLogger(__name__)

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


def _index_conversion(meta: CompanyMeta) -> str | None:
    """The trading currency the index must be converted to, or None if it is already in it."""
    trading = meta.company.trading_currency
    return None if INDEX_CURRENCY[meta.market_index] == trading else trading


def _market(meta: CompanyMeta, fetchers: Fetchers, cache_dir: Path) -> pd.Series:
    """The benchmark's weekly closes in the stock's trading currency, as the spec regresses."""
    index = meta.market_index
    market = load_prices([index], cache_dir=cache_dir, fetcher=fetchers.prices)[index]
    trading = _index_conversion(meta)
    if trading is None:
        return market
    pair = f"{trading}{INDEX_CURRENCY[index]}=X"  # index currency per unit of trading
    fx = load_prices([pair], cache_dir=cache_dir, fetcher=fetchers.prices)[pair]
    both = pd.concat([market, fx], axis=1, join="inner").dropna()
    if both.empty:
        raise DataError(
            f"no dates overlap between {index} and the {pair} history; cannot convert the "
            f"index to {trading} for the beta regression"
        )
    return (both.iloc[:, 0] / both.iloc[:, 1]).rename(index)


def _bracket_miss(
    exc: GrowthOutOfRange, normalized_growth: float, price: float, ccy: str
) -> dict:
    """The DCF section when the price lies beyond the growth bracket: which side, and how far.

    The reason is written here rather than taken from the solver, whose message quotes the
    price in the reporting currency: for a cross-currency listing it would sit next to a
    verdict in another.
    """
    path = growth_path(Assumptions(exc.bound, 0.0, TERMINAL_GROWTH, 1.0))
    word = "higher" if exc.side == "above" else "lower"
    return {
        "status": "unavailable",
        "reason": (
            f"No first-year revenue growth between {GROWTH_BRACKET[0]:.0%} and "
            f"{GROWTH_BRACKET[1]:.0%} reproduces the price of {price:,.2f} {ccy}: it implies "
            f"a {word} rate."
        ),
        "implied_growth_side": exc.side,
        "implied_growth_bound": exc.bound,
        "implied_average_growth_bound": sum(path) / len(path),
        "normalized_growth": normalized_growth,
    }


def _run_dcf(
    meta: CompanyMeta, statements: Statements, prices: pd.DataFrame,
    fetchers: Fetchers, cache_dir: Path,
) -> CompanyResult:
    ccy = meta.company.reporting_currency
    if ccy not in RISK_FREE:
        raise DataError(f"no risk-free rate configured for {ccy}")
    market = _market(meta, fetchers, cache_dir)
    # The filled statements are handed over through a fetcher with refresh=True, in a cache
    # directory of their own: the raw cache must keep the unfilled lines, or the next run would
    # find nothing to default and drop the matching limits.
    return value_company(
        meta.company, prices, market,
        cache_dir=cache_dir / "filled", refresh=True,
        statement_fetcher=lambda _ticker: statements,
        price_fetcher=fetchers.prices, risk_free=RISK_FREE[ccy],
        max_valuation_lag=MAX_VALUATION_LAG_YEARS, beta_currency="trading",
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


# Lines the health section reads but can do without, defaulted to zero like fill_defaults does.
_HEALTH_DEFAULTED = (("balance", "Capital Lease Obligations"),)


def _health_defaults(s: Statements) -> tuple[str, ...]:
    """The defaultable lines health treats as zero, so limits name them even if the DCF fails."""
    return tuple(line for name, line in _HEALTH_DEFAULTED if line not in getattr(s, name).index)


def _reported_revenue(income: pd.DataFrame) -> pd.Series:
    """Revenue for the years actually reported; yfinance often adds a column with none."""
    return _line(income, "Total Revenue").dropna()


def _health_years(s: Statements) -> list[dict]:
    revenue = _reported_revenue(s.income)
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


def _window(income: pd.DataFrame | None) -> list[int]:
    """Years behind the normalized drivers: those with revenue, as in drivers.fiscal_years."""
    if income is None or "Total Revenue" not in income.index:
        return []
    return sorted(pd.Timestamp(year).year for year in _reported_revenue(income).index)


def _age_sentence(income: pd.DataFrame | None, price_date: pd.Timestamp | None) -> list[str]:
    """A sentence when the latest annual statements are over a year older than the price."""
    if income is None or price_date is None or "Total Revenue" not in income.index:
        return []
    revenue = _reported_revenue(income)
    if revenue.empty:
        return []
    year_end = str(pd.Timestamp(max(revenue.index)).date())
    if (price_date - pd.Timestamp(year_end)).days / DAYS_PER_YEAR <= MAX_VALUATION_LAG:
        return []
    return [
        f"Latest annual report is for fiscal year ending {year_end}; balance sheet and share "
        "count are more than 12 months old."
    ]


def _limits(
    meta: CompanyMeta, filled: tuple[str, ...], income: pd.DataFrame | None,
    blank: tuple[str, ...] = (), carried: tuple[Carried, ...] = (),
    price_date: pd.Timestamp | None = None,
) -> list[str]:
    """Every assumption the page rests on; the drivers window only when statements loaded."""
    ccy = meta.company.reporting_currency
    rf_sentence = (
        f"Equity risk premium fixed at 5%; risk-free rate {RISK_FREE[ccy]:.2%} for {ccy} "
        f"(as of {RISK_FREE_DATE})."
        if ccy in RISK_FREE
        else f"No risk-free rate configured for {ccy}."
    )
    converted = _index_conversion(meta)
    beta_sentence = (
        f"Beta regressed on weekly returns against {meta.market_index} over five years"
        + (f", with the index converted to {converted}." if converted else ".")
    )
    limits = [
        *(carried_sentence(*entry) for entry in carried),
        *(blank_years_sentence(line) for line in blank),
        *(limit_sentence(line) for line in filled),
        rf_sentence,
        beta_sentence,
    ]
    limits.extend(_age_sentence(income, price_date))
    years = _window(income)
    if years:
        limits.append(
            f"Normalized drivers are medians over the reported years ({years[0]}–{years[-1]})."
        )
    return limits


def _beyond_bracket(price: float, ccy: str, dcf: dict) -> str:
    above = dcf["implied_growth_side"] == "above"
    more, edge = ("more", "highest") if above else ("less", "lowest")
    return (
        f"At {price:,.2f} {ccy}, the price implies {more} than "
        f"{dcf['implied_average_growth_bound']:.1%} average revenue growth over five years "
        f"\u2014 the {edge} rate this model can solve for "
        f"({dcf['implied_growth_bound']:.0%} in the first year, fading to "
        f"{TERMINAL_GROWTH:.0%}) \u2014 so it cannot say how much {more}. The company's "
        f"normalized history is {dcf['normalized_growth']:.1%}."
    )


def _verdict(price: float, ccy: str, dcf: dict) -> str | None:
    if "implied_growth_side" in dcf:
        return _beyond_bracket(price, ccy, dcf)
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
    meta: CompanyMeta, raw: Statements, prices: pd.DataFrame, fetchers: Fetchers,
    cache_dir: Path, price: float,
) -> tuple[dict, dict, list[str]]:
    """The DCF and health sections plus the limits, each failure contained in its section."""
    health_filled: tuple[str, ...] = ()
    try:
        health = {"status": "ok", "years": _health_years(raw)}
        health_filled = _health_defaults(raw)
    except (DataError, ValuationError) as exc:
        health = _unavailable(exc)
    filled: tuple[str, ...] = ()
    blank: tuple[str, ...] = ()
    carried: tuple[Carried, ...] = ()
    normalized_growth = math.nan
    try:
        statements, filled = fill_defaults(raw)
        statements, carried, blank = fill_blank_years(statements)
        # Read before the DCF so the bracket-miss handler below has nothing left to compute
        # that could raise outside this try.
        normalized_growth = drivers_from(statements, meta.company.ticker).revenue_growth
        dcf = _dcf_section(_run_dcf(meta, statements, prices, fetchers, cache_dir),
                           meta.market_index)
    except GrowthOutOfRange as exc:
        dcf = _bracket_miss(exc, normalized_growth, price, meta.company.trading_currency)
    except (DataError, ValuationError) as exc:
        dcf = _unavailable(exc)
    defaulted = filled + tuple(line for line in health_filled if line not in filled)
    limits = _limits(meta, defaulted, raw.income, blank, carried, prices.index[-1])
    return dcf, health, limits


def _multiples(ticker: str, info: dict, meta: CompanyMeta, cache_dir: Path) -> dict:
    """Peer multiples from the weekly table; absent table or any failure stays contained here."""
    path = cache_dir / PEER_TABLE_FILE
    if not path.exists():
        return dict(UNAVAILABLE_MULTIPLES)
    try:
        market_cap = float(info.get("marketCap") or 0.0)
        if not math.isfinite(market_cap) or market_cap <= 0.0:
            return {"status": "unavailable", "reason": "market capitalization is not reported"}
        return peers_for(
            ticker, meta.industry, meta.sector, market_cap, pd.read_parquet(path),
            company_multiples={
                "ev_ebitda": info.get("enterpriseToEbitda"),
                "ev_sales": info.get("enterpriseToRevenue"),
                "pe": info.get("trailingPE"),
            },
        )
    except Exception as exc:  # the table is a side artifact: it must never fail the analysis
        log.warning("multiples unavailable for %s: %s", ticker, exc)
        return _unavailable(RuntimeError("peer table could not be read"))


def _analyze(ticker: str, fetchers: Fetchers, cache_dir: Path) -> dict:
    info = fetchers.info(ticker)
    meta = company_from_info(ticker, info)
    header = _header(ticker, meta)
    if meta.excluded_reason is not None:
        return {**header, "price": None, "price_date": None, "coverage": "not_covered",
                "verdict": None, "limits": [meta.excluded_reason]}

    # A frame we cannot load is a whole-analysis failure; scaling builds a new frame.
    prices = load_prices([ticker], cache_dir=cache_dir, fetcher=fetchers.prices)
    prices = prices * meta.price_scale
    price = float(prices[ticker].iloc[-1])
    try:
        raw = load_statements(ticker, cache_dir=cache_dir, fetcher=fetchers.statements)
    except DataError as exc:  # no statements at all: neither section can be computed
        dcf = health = _unavailable(exc)
        limits = _limits(meta, (), None)
    else:
        dcf, health, limits = _sections(meta, raw, prices, fetchers, cache_dir, price)
    # Coverage ignores multiples: they depend on a weekly job, not on this company's own data.
    full = dcf["status"] == "ok" and health["status"] == "ok"
    return {
        **header,
        "price": price,
        "price_date": str(prices.index[-1].date()),
        "coverage": "full" if full else "partial",
        "verdict": _verdict(price, meta.company.trading_currency, dcf),
        "dcf": dcf,
        "health": health,
        "multiples": _multiples(ticker, info, meta, cache_dir),
        "limits": limits,
    }


def analyze(ticker: str, *, fetchers: Fetchers, cache_dir: Path) -> dict:
    """The Analysis JSON for one ticker; strict JSON (no NaN). Runs are serialized.

    ``UnknownTicker`` and a stock price that cannot be loaded propagate; every other data or
    model failure is reported inside the section it affects.
    """
    with _LOCK:
        return _clean(_analyze(ticker, fetchers, Path(cache_dir)))  # type: ignore[return-value]
