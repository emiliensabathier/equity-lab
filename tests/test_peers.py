"""Peer table and multiples section, on hand-written mini tables (no network)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from elab.config import PEER_TABLE_FILE, ConstituentSource
from elab.engine import analyze
from elab.peers import build_peer_table, constituents, peers_for
from tests.test_engine import _fetchers, _info


def _frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["ticker", "name", "sector", "industry", "market_cap", "ev_ebitda", "ev_sales",
                 "pe"],
    )


def test_constituents_normalizes_tickers():
    us = ConstituentSource("S&P 500", "u", "Symbol", "")
    uk = ConstituentSource("FTSE 100", "u", "Ticker", ".L")
    fr = ConstituentSource("CAC 40", "u", "Ticker", ".PA")
    pages = {
        "us": [pd.DataFrame({"x": [1]}), pd.DataFrame({"Symbol": ["AAPL", "BRK.B", None]})],
        "uk": [pd.DataFrame({"Ticker": ["III", "BT.A", "BP.", "rr. "]})],
        "fr": [pd.DataFrame({"Ticker": ["AC.PA", "MT.AS", "ml"]})],
    }
    assert constituents(us, lambda _: pages["us"]) == ["AAPL", "BRK-B"]
    assert constituents(uk, lambda _: pages["uk"]) == ["III.L", "BT-A.L", "BP.L", "RR.L"]
    assert constituents(fr, lambda _: pages["fr"]) == ["AC.PA", "MT.AS", "ML.PA"]
    with pytest.raises(ValueError, match="Symbol"):
        constituents(us, lambda _: pages["uk"])


def test_build_peer_table_skips_failures_and_resumes(caplog):
    calls: list[str] = []

    def info(ticker: str) -> dict:
        calls.append(ticker)
        if ticker == "BAD":
            raise RuntimeError("404")
        return {"longName": ticker, "sector": "S", "industry": "I", "marketCap": 10.0,
                "enterpriseToEbitda": 8.0, "trailingPE": None}

    sleeps: list[float] = []
    with caplog.at_level(logging.WARNING):
        table = build_peer_table(["A", "BAD", "B"], info, sleep_s=0.0, sleep=sleeps.append)

    assert list(table["ticker"]) == ["A", "B"]
    assert "fetched_at" in table.columns
    assert "skipped BAD" in caplog.text
    assert len(sleeps) == 3

    stale = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    existing = table.assign(fetched_at=[table["fetched_at"][0], stale])
    calls.clear()
    again = build_peer_table(["A", "B"], info, sleep_s=0.0, existing=existing)
    assert calls == ["B"]  # A is fresh and reused, B is stale and refetched
    assert list(again["ticker"]) == ["A", "B"]


def _peers() -> pd.DataFrame:
    rows = [("SELF", "Self", "Tech", "Machinery", 100.0, 10.0, 2.0, 20.0)]
    rows += [(f"M{i}", f"M{i}", "Tech", "Machinery", 100.0 * (i + 2), 8.0 + i, 1.0, 15.0)
             for i in range(10)]
    rows += [(f"T{i}", f"T{i}", "Tech", "Software", 100.0 * (i + 1), 20.0, 5.0, 30.0)
             for i in range(5)]
    return _frame(rows)


def test_peers_for_prefers_industry_then_sector():
    got = peers_for("SELF", "Machinery", "Tech", 100.0, _peers())
    assert got["status"] == "ok" and got["basis"] == "industry"
    assert len(got["peers"]) == 8
    assert {p["ticker"][0] for p in got["peers"]} == {"M"}
    assert got["peers"][0]["ticker"] == "M0"  # nearest in log market cap comes first
    assert "EV/EBITDA, not EV/EBIT" in got["note"]

    rare = peers_for("SELF", "Rare", "Tech", 100.0, _peers(), company_multiples={"pe": 20.0})
    assert rare["basis"] == "sector"
    assert rare["company"]["pe"] == 20.0

    none = peers_for("SELF", "Rare", "Nothing", 100.0, _peers())
    assert none == {
        "status": "unavailable",
        "reason": "fewer than four comparable companies in the peer table",
    }


def test_peers_for_excludes_company_itself():
    got = peers_for("SELF", "Machinery", "Tech", 100.0, _peers())
    assert "SELF" not in [p["ticker"] for p in got["peers"]]

    only_self = _frame([("SELF", "Self", "Tech", "Machinery", 100.0, 1.0, 1.0, 1.0)] * 5)
    assert peers_for("SELF", "Machinery", "Tech", 100.0, only_self)["status"] == "unavailable"


def test_peers_for_median_ignores_negative_pe():
    rows = [
        ("A", "A", "S", "I", 100.0, 10.0, 1.0, -5.0),
        ("B", "B", "S", "I", 100.0, 20.0, 2.0, 10.0),
        ("C", "C", "S", "I", 100.0, float("nan"), 3.0, 30.0),
        ("D", "D", "S", "I", 100.0, 30.0, 4.0, 0.0),
    ]
    got = peers_for("X", "I", "S", 100.0, _frame(rows))
    assert got["median"] == {"ev_ebitda": 20.0, "ev_sales": 2.5, "pe": 20.0}
    assert got["peers"][0]["pe"] is None


def test_engine_multiples_unavailable_without_table(tmp_path):
    got = analyze("ACME.PA", fetchers=_fetchers(), cache_dir=tmp_path)
    assert got["multiples"] == {"status": "unavailable", "reason": "peer table not built yet"}


def test_engine_multiples_from_table_and_isolated_failures(tmp_path):
    rows = [(f"P{i}", f"P{i}", "Industrials", "Machinery", 1e9 * (i + 1), 8.0, 1.0, 12.0)
            for i in range(5)]
    _frame(rows).assign(fetched_at="2026-10-16T00:00:00+00:00").to_parquet(
        tmp_path / PEER_TABLE_FILE
    )
    info = _info(marketCap=3e9, enterpriseToEbitda=9.0, trailingPE=14.0)

    got = analyze("ACME.PA", fetchers=_fetchers(info=info), cache_dir=tmp_path)
    assert got["coverage"] == "full"
    assert got["multiples"]["status"] == "ok"
    assert got["multiples"]["company"]["ev_ebitda"] == 9.0
    assert got["multiples"]["median"]["ev_ebitda"] == 8.0

    no_cap = analyze("ACME.PA", fetchers=_fetchers(), cache_dir=tmp_path)
    assert no_cap["multiples"] == {
        "status": "unavailable", "reason": "market capitalization is not reported",
    }

    (tmp_path / PEER_TABLE_FILE).write_bytes(b"not parquet")
    broken = analyze("ACME.PA", fetchers=_fetchers(info=info), cache_dir=tmp_path)
    assert broken["multiples"]["status"] == "unavailable"
    assert broken["coverage"] == "full"


def test_note_mentions_listing_currencies():
    got = peers_for("SELF", "Machinery", "Tech", 100.0, _peers())
    assert got["note"].startswith("Multiples use EV/EBITDA, not EV/EBIT: peer figures come")
    assert "listing currencies" in got["note"]


def test_build_peer_table_rejects_empty_info_and_tolerates_bad_existing(caplog):
    def info(ticker: str) -> dict:
        return {} if ticker == "EMPTY" else {"marketCap": 5.0, "industry": "I"}

    with caplog.at_level(logging.WARNING):
        table = build_peer_table(["EMPTY", "OK"], info, sleep_s=0.0, sleep=lambda _: None)
    assert list(table["ticker"]) == ["OK"]
    assert "skipped EMPTY" in caplog.text

    bad = pd.DataFrame({"ticker": ["OK", "Z"], "fetched_at": ["garbage", None]})
    again = build_peer_table(["OK", "Z"], info, sleep_s=0.0, existing=bad, sleep=lambda _: None)
    assert list(again["ticker"]) == ["OK", "Z"]
    assert again["market_cap"].tolist() == [5.0, 5.0]

    no_col = pd.DataFrame({"ticker": ["OK"]})
    got = build_peer_table(["OK"], info, sleep_s=0.0, existing=no_col, sleep=lambda _: None)
    assert list(got["ticker"]) == ["OK"]


def test_peers_for_handles_duplicate_index_labels():
    table = pd.concat([_peers(), _peers()])
    got = peers_for("SELF", "Machinery", "Tech", 100.0, table)
    assert len(got["peers"]) == 8


def test_peers_for_company_negative_multiple_is_null():
    got = peers_for("SELF", "Machinery", "Tech", 100.0, _peers(),
                    company_multiples={"pe": -4.0, "ev_ebitda": 9.0})
    assert got["company"]["pe"] is None and got["company"]["ev_ebitda"] == 9.0
