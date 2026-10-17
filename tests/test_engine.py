"""The engine end to end, on hand-built statements and synthetic weekly prices (no network)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from vlab.wacc import EQUITY_RISK_PREMIUM

from elab.config import RISK_FREE
from elab.engine import Fetchers, analyze
from elab.resolve import UnknownTicker
from tests.conftest import make_statements

WEEKS = pd.date_range(end="2026-10-16", periods=260, freq="W-FRI")
LAST_PRICE = 20.0


def _series(seed: int, last: float) -> pd.Series:
    rng = np.random.default_rng(seed)
    market = rng.normal(0.001, 0.02, len(WEEKS))
    own = 1.2 * market + rng.normal(0.0, 0.01, len(WEEKS))
    path = np.cumprod(1.0 + own)
    return pd.Series(path / path[-1] * last, index=WEEKS)


def _index_series() -> pd.Series:
    rng = np.random.default_rng(7)
    market = rng.normal(0.001, 0.02, len(WEEKS))
    return pd.Series(np.cumprod(1.0 + market) * 4000.0, index=WEEKS)


def _info(**overrides: object) -> dict:
    info = {
        "quoteType": "EQUITY",
        "currency": "EUR",
        "financialCurrency": "EUR",
        "exchange": "PAR",
        "longName": "Acme SA",
        "sector": "Industrials",
        "industry": "Machinery",
    }
    info.update(overrides)
    return info


def _fetchers(
    *, info: dict | None = None, statements=None, last_price: float = LAST_PRICE
) -> Fetchers:
    stock = _series(7, last_price)
    index = _index_series()

    def prices(tickers: list[str], period: str, interval: str) -> pd.DataFrame:
        columns = {}
        for ticker in tickers:
            if ticker.endswith("=X"):
                columns[ticker] = pd.Series(1.1, index=WEEKS)
            else:
                columns[ticker] = index if ticker.startswith("^") else stock
        return pd.DataFrame(columns)

    return Fetchers(
        statements=lambda ticker: statements if statements is not None else make_statements(),
        prices=prices,
        info=lambda ticker: info if info is not None else _info(),
    )


def _drop(frame: pd.DataFrame, line: str) -> pd.DataFrame:
    return frame.drop(index=line)


def test_analyze_full_coverage_returns_verdict_and_sections(tmp_path):
    got = analyze("ACME.PA", fetchers=_fetchers(), cache_dir=tmp_path)

    assert got["coverage"] == "full"
    assert got["name"] == "Acme SA"
    assert got["reporting_currency"] == got["trading_currency"] == "EUR"
    assert got["price"] == pytest.approx(LAST_PRICE)
    assert got["price_date"] == "2026-10-16"
    dcf = got["dcf"]
    assert dcf["status"] == "ok"
    assert dcf["beta_index"] == "^FCHI"
    assert set(dcf["implied_average_growth_at_lower_wacc"]) == {"0.01", "0.02"}
    assert dcf["fiscal_years"][-1] == "2025-12-31"
    assert len(dcf["sensitivity"]["rows"]) == 5
    assert len(dcf["sensitivity"]["values"]) == 5
    assert len(dcf["margin_sensitivity"]["cols"]) == 5
    assert got["verdict"] == (
        f"At {LAST_PRICE:,.2f} EUR, the market prices {dcf['implied_average_growth']:.1%} "
        f"average revenue growth over five years; the company's normalized history is "
        f"{dcf['normalized_growth']:.1%}."
    )
    health = got["health"]
    assert health["status"] == "ok"
    latest = health["years"][-1]
    assert latest["fiscal_year"] == "2025-12-31"
    assert latest["revenue"] == 1000.0
    assert latest["ebit_margin"] == pytest.approx(0.2)
    assert latest["fcf"] == pytest.approx(230.0 - 60.0)
    assert latest["net_debt_to_ebitda"] == pytest.approx((300.0 - 80.0 - 120.0) / 250.0)
    assert got["multiples"] == {"status": "unavailable", "reason": "peer table not built yet"}
    assert got["limits"] == [
        f"Equity risk premium fixed at 5%; risk-free rate {RISK_FREE['EUR']:.2%} for EUR "
        "(as of 2026-10-16).",
        "Beta regressed on weekly returns against ^FCHI over five years.",
        "Normalized drivers are medians over the reported years (2022–2025).",
    ]


def test_analyze_bank_is_not_covered_without_fetching_statements(tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("must not fetch")

    fetchers = Fetchers(
        statements=boom, prices=boom, info=lambda t: _info(sector="Financial Services")
    )
    got = analyze("BNP.PA", fetchers=fetchers, cache_dir=tmp_path)

    assert got["coverage"] == "not_covered"
    assert got["verdict"] is None
    assert "dcf" not in got and "health" not in got
    assert got["limits"] == [
        "Financial Services companies are not covered: a free-cash-flow DCF does not apply "
        "to balance-sheet businesses."
    ]


def test_analyze_unknown_ticker_propagates(tmp_path):
    fetchers = _fetchers(info={"quoteType": "ETF", "currency": "USD"})
    with pytest.raises(UnknownTicker):
        analyze("SPY", fetchers=fetchers, cache_dir=tmp_path)


def test_analyze_missing_core_line_marks_dcf_unavailable_keeps_health(tmp_path):
    base = make_statements()
    statements = make_statements(income=_drop(base.income, "Tax Provision"))
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["coverage"] == "partial"
    assert got["verdict"] is None
    assert got["dcf"]["status"] == "unavailable"
    assert "Tax Provision" in got["dcf"]["reason"]
    assert got["health"]["status"] == "ok"
    assert len(got["health"]["years"]) == 4


def test_analyze_missing_operating_cash_flow_marks_health_unavailable(tmp_path):
    base = make_statements()
    statements = make_statements(cashflow=_drop(base.cashflow, "Operating Cash Flow"))
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["health"]["status"] == "unavailable"
    assert "Operating Cash Flow" in got["health"]["reason"]
    assert got["dcf"]["status"] == "ok"
    assert got["coverage"] == "partial"


def test_analyze_lists_defaulted_lines_in_limits(tmp_path):
    base = make_statements()
    balance = _drop(_drop(base.balance, "Minority Interest"), "Inventory")
    statements = make_statements(balance=balance)
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["limits"][:2] == [
        "Minority Interest not reported: treated as zero.",
        "Inventory not reported: treated as zero.",
    ]
    assert got["limits"][2].startswith("Equity risk premium fixed at 5%")
    assert got["dcf"]["status"] == "ok"


def test_analyze_pence_listing_scales_price(tmp_path):
    info = _info(currency="GBp", financialCurrency="GBP", exchange="LSE")
    got = analyze(
        "ACME.L", fetchers=_fetchers(info=info, last_price=2500.0), cache_dir=tmp_path
    )

    assert got["price"] == pytest.approx(25.0)
    assert got["trading_currency"] == "GBP"
    assert got["dcf"]["status"] == "ok"
    assert got["verdict"].startswith("At 25.00 GBP")
    assert got["dcf"]["beta_index"] == "^FTSE"


def test_analyze_implied_growth_outside_bracket_is_unavailable_not_raised(tmp_path):
    got = analyze("ACME.PA", fetchers=_fetchers(last_price=1e6), cache_dir=tmp_path)

    assert got["dcf"]["status"] == "unavailable"
    assert got["health"]["status"] == "ok"


def test_a_price_above_the_bracket_still_gets_a_verdict_naming_the_side(tmp_path):
    # The ceiling is 50% first-year growth fading to 2%: a 26% five-year average.
    got = analyze("ACME.PA", fetchers=_fetchers(last_price=1e6), cache_dir=tmp_path)

    dcf = got["dcf"]
    assert dcf["implied_growth_side"] == "above"
    assert dcf["implied_average_growth_bound"] == pytest.approx(0.26)
    assert dcf["implied_growth_bound"] == pytest.approx(0.50)
    assert got["verdict"] == (
        "At 1,000,000.00 EUR, the price implies more than 26.0% average revenue growth over "
        "five years \u2014 the highest rate this model can solve for (50% in the first year, "
        "fading to 2%) \u2014 so it cannot say how much more. The company's normalized history "
        f"is {dcf['normalized_growth']:.1%}."
    )


def test_a_price_below_the_bracket_gets_a_verdict_naming_the_floor(tmp_path):
    got = analyze("ACME.PA", fetchers=_fetchers(last_price=0.01), cache_dir=tmp_path)

    assert got["dcf"]["implied_growth_side"] == "below"
    assert got["dcf"]["implied_average_growth_bound"] == pytest.approx(-0.09)
    assert got["verdict"].startswith(
        "At 0.01 EUR, the price implies less than -9.0% average revenue growth over five "
        "years \u2014 the lowest rate this model can solve for (-20% in the first year, "
        "fading to 2%) \u2014 so it cannot say how much less."
    )


def test_an_adr_beyond_the_bracket_states_the_price_in_its_trading_currency(tmp_path):
    # vlab's solver message quotes the price in the reporting currency (EUR here): next to a
    # verdict in USD the reader would see two prices for one share.
    info = _info(currency="USD", financialCurrency="EUR", exchange="NYQ")
    got = analyze("ACME", fetchers=_fetchers(info=info, last_price=1e6), cache_dir=tmp_path)

    reason = got["dcf"]["reason"]
    assert "1,000,000.00 USD" in reason
    assert "EUR" not in reason
    assert got["verdict"].startswith("At 1,000,000.00 USD")


def test_a_failure_while_describing_a_bracket_miss_stays_in_the_section(tmp_path, monkeypatch):
    # The normalized growth the verdict quotes is read before the DCF runs, so a data error
    # there is handled like any other instead of escaping from the bracket-miss handler.
    from vlab.errors import DataError

    import elab.engine as engine

    def broken(*args, **kwargs):
        raise DataError("boom")

    monkeypatch.setattr(engine, "drivers_from", broken)
    got = analyze("ACME.PA", fetchers=_fetchers(last_price=1e6), cache_dir=tmp_path)

    assert got["dcf"] == {"status": "unavailable", "reason": "boom"}
    assert got["verdict"] is None
    assert got["health"]["status"] == "ok"


def _with_years(statements, years: list[str]):
    index = pd.to_datetime(years)
    return make_statements(
        income=statements.income.set_axis(index, axis=1),
        cashflow=statements.cashflow.set_axis(index, axis=1),
        balance=statements.balance.set_axis(index, axis=1),
    )


SEPTEMBER_YEARS = ["2025-09-30", "2024-09-30", "2023-09-30", "2022-09-30"]


def test_a_september_year_end_priced_before_the_next_filing_is_valued(tmp_path):
    # Priced 2026-10-16 against a 2025-09-30 year-end: the FY2026 report is not out yet, so a
    # lag just over a year is the normal state for weeks every autumn.
    statements = _with_years(make_statements(), SEPTEMBER_YEARS)
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["dcf"]["status"] == "ok"
    assert got["coverage"] == "full"
    assert (
        "Latest annual report is for fiscal year ending 2025-09-30; balance sheet and share "
        "count are more than 12 months old."
    ) in got["limits"]


def test_statements_within_a_year_add_no_age_sentence(tmp_path):
    got = analyze("ACME.PA", fetchers=_fetchers(), cache_dir=tmp_path)

    assert not any("more than 12 months old" in line for line in got["limits"])


def test_statements_more_than_eighteen_months_old_are_refused(tmp_path):
    statements = _with_years(
        make_statements(), ["2025-03-31", "2024-03-31", "2023-03-31", "2022-03-31"]
    )
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["dcf"]["status"] == "unavailable"
    assert "lag" in got["dcf"]["reason"]


def _fx_series() -> pd.Series:
    rng = np.random.default_rng(11)
    return pd.Series(1.1 * np.cumprod(1.0 + rng.normal(0.0, 0.01, len(WEEKS))), index=WEEKS)


def _priced(series: dict, info: dict) -> Fetchers:
    def prices(tickers, period, interval):
        return pd.DataFrame({ticker: series[ticker] for ticker in tickers})

    return Fetchers(statements=lambda t: make_statements(), prices=prices, info=lambda t: info)


def test_beta_uses_the_stock_and_index_as_quoted_when_both_trade_in_one_currency(tmp_path):
    # An ADR quoted in USD, reporting in EUR, benchmarked on the S&P 500 (USD): the spec's
    # trading-currency rule, so no exchange rate enters the regression; the currency
    # difference goes through the risk-free rate instead.
    from vlab.wacc import levered_beta

    fx = _fx_series()
    stock, index = _series(7, LAST_PRICE), _index_series()
    info = _info(currency="USD", financialCurrency="EUR", exchange="NYQ")
    fetchers = _priced({"EURUSD=X": fx, "^GSPC": index, "ACME": stock}, info)
    got = analyze("ACME", fetchers=fetchers, cache_dir=tmp_path)

    assert got["dcf"]["status"] == "ok"
    assert got["dcf"]["beta"] == pytest.approx(levered_beta(stock, index))
    assert (
        "Beta regressed on weekly returns against ^GSPC over five years." in got["limits"]
    )


def test_an_index_in_another_currency_is_converted_to_the_stocks_trading_currency(tmp_path):
    # A London listing quoted and reported in USD, benchmarked on the FTSE 100 (GBP).
    from vlab.wacc import levered_beta

    fx = _fx_series()
    stock, index = _series(7, LAST_PRICE), _index_series()
    info = _info(currency="USD", financialCurrency="USD", exchange="LSE")
    got = analyze("ACME.L", fetchers=_priced({"USDGBP=X": fx, "^FTSE": index, "ACME.L": stock},
                                             info), cache_dir=tmp_path)

    assert got["dcf"]["beta"] == pytest.approx(levered_beta(stock, index / fx))
    assert (
        "Beta regressed on weekly returns against ^FTSE over five years, with the index "
        "converted to USD."
    ) in got["limits"]


def test_a_pence_listing_reporting_in_euros_is_regressed_in_pounds(tmp_path):
    # Unilever: quoted in GBp, reporting in EUR, on the FTSE 100 (GBP). Stock and index already
    # share a currency, so neither is converted (the spec's 0.55, not 0.74 in EUR).
    from vlab.wacc import levered_beta

    stock, index = _series(7, LAST_PRICE * 100), _index_series()
    info = _info(currency="GBp", financialCurrency="EUR", exchange="LSE")
    got = analyze("ACME.L", fetchers=_priced({"EURGBP=X": _fx_series(), "^FTSE": index,
                                              "ACME.L": stock}, info), cache_dir=tmp_path)

    assert got["dcf"]["beta"] == pytest.approx(levered_beta(stock / 100, index))


def test_the_fallback_index_is_converted_for_a_listing_outside_the_euro(tmp_path):
    from vlab.wacc import levered_beta

    fx = _fx_series()
    stock, index = _series(7, LAST_PRICE), _index_series()
    info = _info(currency="NOK", financialCurrency="NOK", exchange="OSL")
    got = analyze("ACME.OL", fetchers=_priced({"NOKEUR=X": fx, "^STOXX": index,
                                               "ACME.OL": stock}, info), cache_dir=tmp_path)

    assert got["dcf"]["beta"] == pytest.approx(levered_beta(stock, index / fx))


def test_an_index_and_rate_with_no_common_dates_is_named_in_the_reason(tmp_path):
    fx = pd.Series(1.1, index=WEEKS - pd.Timedelta(days=3))
    info = _info(currency="USD", financialCurrency="USD", exchange="LSE")
    got = analyze("ACME.L", fetchers=_priced({"USDGBP=X": fx, "^FTSE": _index_series(),
                                              "ACME.L": _series(7, LAST_PRICE)}, info),
                  cache_dir=tmp_path)

    assert got["dcf"]["status"] == "unavailable"
    assert got["dcf"]["reason"] == (
        "no dates overlap between ^FTSE and the USDGBP=X history; cannot convert the index "
        "to USD for the beta regression"
    )


def test_a_latest_year_left_blank_in_interest_and_leases_carries_the_prior_year(tmp_path):
    # Apple left interest and leases blank for its latest years. Zero there showed a 0% cost
    # of debt that the earlier years contradict; the most recent reported value is used.
    base = make_statements()
    income, balance = base.income.copy(), base.balance.copy()
    income.loc["Interest Expense"] = [np.nan, 12.0, 10.0, 10.0]
    balance.loc["Capital Lease Obligations", balance.columns[0]] = np.nan
    statements = make_statements(income=income, balance=balance)
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["dcf"]["status"] == "ok"
    assert got["dcf"]["cost_of_debt"] == pytest.approx(12.0 / 300.0)
    assert got["limits"][:2] == [
        "Interest Expense not reported for 2025: 2024 value used.",
        "Capital Lease Obligations not reported for 2025: 2024 value used.",
    ]


def test_a_latest_year_left_blank_in_a_core_line_marks_dcf_unavailable(tmp_path):
    base = make_statements()
    balance = base.balance.copy()
    balance.loc["Total Debt", balance.columns[0]] = np.nan
    got = analyze("ACME.PA", fetchers=_fetchers(statements=make_statements(balance=balance)),
                  cache_dir=tmp_path)

    assert got["dcf"]["status"] == "unavailable"
    assert "Total Debt" in got["dcf"]["reason"]


def test_analyze_uses_risk_free_of_reporting_currency(tmp_path):
    # Trades in USD, reports in EUR: the euro rate applies, since the cash flows are in euros.
    info = _info(currency="USD", financialCurrency="EUR", exchange="NYQ")
    got = analyze("ACME", fetchers=_fetchers(info=info), cache_dir=tmp_path)

    dcf = got["dcf"]
    assert dcf["status"] == "ok"
    assert dcf["beta_index"] == "^GSPC"
    assert dcf["cost_of_equity"] == pytest.approx(
        RISK_FREE["EUR"] + dcf["beta"] * EQUITY_RISK_PREMIUM
    )


def test_analyze_unknown_currency_marks_dcf_unavailable(tmp_path):
    info = _info(currency="PLN", financialCurrency="PLN", exchange="WSE")
    got = analyze("ACME.WA", fetchers=_fetchers(info=info), cache_dir=tmp_path)

    assert got["dcf"] == {
        "status": "unavailable",
        "reason": "no risk-free rate configured for PLN",
    }
    assert got["health"]["status"] == "ok"
    assert got["limits"][0] == "No risk-free rate configured for PLN."


def test_analyze_does_not_mutate_fetched_statements(tmp_path):
    statements = make_statements()
    before = statements.balance.copy()
    analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)
    pd.testing.assert_frame_equal(statements.balance, before)


def test_analysis_json_is_serializable(tmp_path):
    base = make_statements()
    income = base.income.copy()
    income.loc["EBIT", income.columns[-1]] = np.nan
    cashflow = base.cashflow.copy()
    cashflow.loc["Depreciation And Amortization", cashflow.columns[0]] = -500.0
    statements = make_statements(income=income, cashflow=cashflow)
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    text = json.dumps(got, allow_nan=False)
    years = json.loads(text)["health"]["years"]
    assert years[0]["ebit_margin"] is None
    assert years[-1]["net_debt_to_ebitda"] is None


def test_clean_replaces_nan_and_infinities_recursively():
    from elab.engine import _clean

    got = _clean({"a": [np.nan, 1.0, (np.inf,)], "b": {"c": -np.inf}})
    assert got == {"a": [None, 1.0, [None]], "b": {"c": None}}


def test_analyze_stock_price_failure_propagates(tmp_path):
    from vlab.errors import DataError

    def no_prices(tickers, period, interval):
        return pd.DataFrame()

    fetchers = Fetchers(statements=lambda t: make_statements(), prices=no_prices,
                        info=lambda t: _info())
    with pytest.raises(DataError):
        analyze("ACME.PA", fetchers=fetchers, cache_dir=tmp_path)


def test_analyze_statements_failure_marks_both_sections_unavailable(tmp_path):
    empty = make_statements(income=pd.DataFrame())
    got = analyze("ACME.PA", fetchers=_fetchers(statements=empty), cache_dir=tmp_path)

    assert got["coverage"] == "partial"
    assert got["dcf"]["status"] == got["health"]["status"] == "unavailable"
    assert "no financial statements" in got["dcf"]["reason"]
    assert len(got["limits"]) == 2


def _with_unreported_year(base):
    """A fifth, older column as yfinance often returns it: other lines present, no revenue."""
    extra = pd.Timestamp("2021-12-31")

    def widen(frame: pd.DataFrame) -> pd.DataFrame:
        wider = frame.copy()
        wider[extra] = frame.iloc[:, -1]
        return wider

    income = widen(base.income)
    income.loc["Total Revenue", extra] = np.nan
    return make_statements(
        income=income, cashflow=widen(base.cashflow), balance=widen(base.balance)
    )


def test_analyze_driver_window_ignores_year_without_revenue(tmp_path):
    statements = _with_unreported_year(make_statements())
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["limits"][-1] == (
        "Normalized drivers are medians over the reported years (2022–2025)."
    )


def test_analyze_health_has_no_phantom_year_without_revenue(tmp_path):
    statements = _with_unreported_year(make_statements())
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    years = [year["fiscal_year"] for year in got["health"]["years"]]
    assert years == ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]


def test_analyze_lists_lease_default_health_relies_on_when_dcf_fails(tmp_path):
    base = make_statements()
    statements = make_statements(
        income=_drop(base.income, "Tax Provision"),
        balance=_drop(base.balance, "Capital Lease Obligations"),
    )
    got = analyze("ACME.PA", fetchers=_fetchers(statements=statements), cache_dir=tmp_path)

    assert got["dcf"]["status"] == "unavailable"
    assert got["health"]["status"] == "ok"
    assert got["limits"][0] == "Capital Lease Obligations not reported: treated as zero."
    assert got["limits"].count("Capital Lease Obligations not reported: treated as zero.") == 1


def test_every_benchmark_has_a_quote_currency():
    from elab.config import FALLBACK_INDEX, INDEX_BY_SUFFIX, INDEX_CURRENCY

    assert {*INDEX_BY_SUFFIX.values(), FALLBACK_INDEX} <= INDEX_CURRENCY.keys()
