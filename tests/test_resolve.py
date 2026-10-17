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


def test_search_drops_dotless_equity_on_non_us_exchange():
    quotes = [_quote("NSRGY", "PNK"), _quote("NSRGF", "OQB"), _quote("NSRGY", "NYQ")]
    assert [m.exchange for m in search("nestle", lambda q: quotes)] == ["NYQ"]


def test_gbx_is_pence_for_trading_and_reporting():
    info = {"quoteType": "EQUITY", "longName": "X", "currency": "GBX",
            "financialCurrency": "GBX", "exchange": "LSE"}
    meta = company_from_info("X.L", info)
    assert (meta.company.trading_currency, meta.company.reporting_currency) == ("GBP", "GBP")
    assert meta.price_scale == 0.01


@pytest.mark.parametrize("code", ["ZAc", "ILA"])
def test_other_subunit_currencies_are_refused(code):
    info = {"quoteType": "EQUITY", "longName": "X", "currency": code}
    with pytest.raises(UnknownTicker):
        company_from_info("X.JO", info)


def test_us_exchange_wins_over_dots_in_ticker():
    info = {"quoteType": "EQUITY", "longName": "Berkshire", "currency": "USD",
            "exchange": "NYQ"}
    assert company_from_info("BRK-B", info).market_index == "^GSPC"
    assert company_from_info("BRK.B", info).market_index == "^GSPC"


def test_multi_dot_ticker_uses_last_suffix():
    info = {"quoteType": "EQUITY", "longName": "X", "currency": "GBP", "exchange": "LSE"}
    assert company_from_info("A.B.L", info).market_index == "^FTSE"


def test_search_fills_from_local_table_when_yahoo_returns_only_etfs():
    quotes = [_quote("TTEF", "NYQ", "ETF")]
    local = [("TTE.PA", "TotalEnergies SE"), ("AAPL", "Apple Inc.")]
    got = search("total", lambda q: quotes, lambda: local)
    assert got == [Match("TTE.PA", "TotalEnergies SE", "")]


def test_search_local_matches_word_prefix_and_ticker_prefix_case_insensitively():
    local = [("SAP.DE", "SAP SE"), ("BAS.DE", "BASF SE"), ("AIR.PA", "Airbus SE")]
    energies = [("X", "Total Energies")]
    assert [m.ticker for m in search("energ", lambda q: [], lambda: energies)] == ["X"]
    assert [m.ticker for m in search("bas", lambda q: [], lambda: local)] == ["BAS.DE"]
    assert [m.ticker for m in search("ASF", lambda q: [], lambda: local)] == []  # not a prefix
    assert [m.ticker for m in search("air", lambda q: [], lambda: local)] == ["AIR.PA"]


def test_search_local_skips_yahoo_duplicates_and_respects_cap():
    quotes = [_quote("TTE.PA", "PAR")]
    local = [("TTE.PA", "TotalEnergies SE")] + [(f"T{i}", f"Total {i}") for i in range(12)]
    got = search("total", lambda q: quotes, lambda: local)
    assert got[0].ticker == "TTE.PA" and got[0].exchange == "PAR"
    assert len(got) == 8 and len({m.ticker for m in got}) == 8


def test_local_peer_names_reads_table_and_tolerates_missing_file(tmp_path):
    import pandas as pd

    from elab.resolve import local_peer_names

    assert local_peer_names(tmp_path)() == []
    pd.DataFrame({"ticker": ["TTE.PA"], "name": ["TotalEnergies SE"], "pe": [9.0]}).to_parquet(
        tmp_path / "peers.parquet"
    )
    assert local_peer_names(tmp_path)() == [("TTE.PA", "TotalEnergies SE")]
