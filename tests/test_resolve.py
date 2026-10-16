"""Name resolution and company description, with an injected searcher (no network)."""

from __future__ import annotations

import pytest
from vlab.errors import DataError

from elab.resolve import Match, UnknownTicker, company_from_info, search


def _quote(symbol: str, exchange: str, quote_type: str = "EQUITY") -> dict:
    return {"symbol": symbol, "shortname": symbol + " name", "exchange": exchange,
            "quoteType": quote_type}


def test_search_keeps_us_and_european_equities_only():
    quotes = [
        _quote("TTEF", "NYQ", "ETF"),
        _quote("8604.T", "JPX"),
        _quote("TTE", "NYQ"),
        _quote("TTE.PA", "PAR"),
    ]
    got = search("total", lambda q: quotes)
    assert got == [Match("TTE", "TTE name", "NYQ"), Match("TTE.PA", "TTE.PA name", "PAR")]


def test_search_caps_results_at_eight():
    quotes = [_quote(f"A{i}", "NMS") for i in range(12)]
    assert len(search("apple", lambda q: quotes)) == 8


@pytest.mark.parametrize("query", ["a", " a ", "", "x" * 61])
def test_search_rejects_bad_length_query(query):
    def boom(q):
        raise AssertionError("searcher must not be called")

    assert search(query, boom) == []


def test_company_from_info_converts_pence_listing():
    info = {"quoteType": "EQUITY", "longName": "Shell plc", "currency": "GBp",
            "financialCurrency": "GBP", "sector": "Energy", "industry": "Oil", "exchange": "LSE"}
    meta = company_from_info("SHEL.L", info)
    assert meta.company.trading_currency == "GBP"
    assert meta.company.reporting_currency == "GBP"
    assert meta.price_scale == 0.01
    assert meta.market_index == "^FTSE"
    assert meta.excluded_reason is None


def test_company_from_info_keeps_adr_currencies_distinct():
    info = {"quoteType": "EQUITY", "shortName": "SAP", "currency": "USD",
            "financialCurrency": "EUR", "sector": "Technology", "exchange": "NYQ"}
    meta = company_from_info("SAP", info)
    assert meta.company.reporting_currency == "EUR"
    assert meta.company.trading_currency == "USD"
    assert meta.company.name == "SAP"
    assert meta.market_index == "^GSPC"
    assert meta.price_scale == 1.0


def test_company_from_info_defaults_reporting_to_trading_currency():
    info = {"quoteType": "EQUITY", "longName": "X", "currency": "EUR"}
    assert company_from_info("X.PA", info).company.reporting_currency == "EUR"


def test_company_from_info_flags_bank_as_not_covered():
    info = {"quoteType": "EQUITY", "longName": "Bank", "currency": "USD",
            "sector": "Financial Services"}
    meta = company_from_info("JPM", info)
    assert meta.excluded_reason == (
        "Financial Services companies are not covered: a free-cash-flow DCF does not apply "
        "to balance-sheet businesses."
    )


def test_unknown_suffix_falls_back_to_stoxx():
    info = {"quoteType": "EQUITY", "longName": "Y", "currency": "JPY"}
    assert company_from_info("7203.T", info).market_index == "^STOXX"


@pytest.mark.parametrize("info", [{}, {"quoteType": "ETF", "currency": "USD"}])
def test_non_equity_or_empty_info_raises_unknown_ticker(info):
    with pytest.raises(UnknownTicker) as exc:
        company_from_info("ZZZ", info)
    assert isinstance(exc.value, DataError)
