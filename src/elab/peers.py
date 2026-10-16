"""Weekly peer table (index constituents + yfinance summary multiples) and the multiples section.

The table is a cache artifact built by a weekly job, never committed. Peer multiples come from
yfinance's summary ``info`` (EV/EBITDA, EV/Sales, trailing P/E), so the section compares EV/EBITDA,
not EV/EBIT, and says so.
"""

from __future__ import annotations

import io
import logging
import math
import statistics
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pandas as pd

from elab.config import (
    INDEX_BY_SUFFIX,
    MAX_PEERS,
    MIN_PEERS,
    PEER_REFRESH_DAYS,
    ConstituentSource,
)

log = logging.getLogger(__name__)

COLUMNS = (
    "ticker", "name", "sector", "industry", "market_cap", "ev_ebitda", "ev_sales", "pe",
    "fetched_at",
)
_MULTIPLES = ("ev_ebitda", "ev_sales", "pe")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_NOTE = "Multiples use EV/EBITDA, not EV/EBIT: peer figures come from summary data."


def read_html_tables(url: str) -> list[pd.DataFrame]:
    """Default reader: Wikipedia rejects urllib's bare User-Agent, so send a browser-like one."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https URLs
        html = response.read().decode("utf-8")
    return pd.read_html(io.StringIO(html))


def _normalize(raw: str, suffix: str) -> str:
    """Yahoo symbol: ``BRK.B`` -> ``BRK-B``; the suffix is added unless the page printed it."""
    ticker = raw.strip().upper()
    _, dot, tail = ticker.rpartition(".")
    if dot and f".{tail}" in INDEX_BY_SUFFIX:
        return ticker
    return ticker.replace(".", "-") + suffix


def constituents(
    source: ConstituentSource,
    reader: Callable[[str], list[pd.DataFrame]] = read_html_tables,
) -> list[str]:
    """Normalized tickers of an index, from the first page table with the ticker column."""
    for table in reader(source.url):
        if source.ticker_column in table.columns:
            raw = table[source.ticker_column].dropna().astype(str)
            return [_normalize(value, source.suffix) for value in raw if value.strip()]
    raise ValueError(f"no table with a {source.ticker_column!r} column at {source.url}")


def _number(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _row(ticker: str, info: dict, fetched_at: str) -> dict:
    return {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": _number(info.get("marketCap")),
        "ev_ebitda": _number(info.get("enterpriseToEbitda")),
        "ev_sales": _number(info.get("enterpriseToRevenue")),
        "pe": _number(info.get("trailingPE")),
        "fetched_at": fetched_at,
    }


def build_peer_table(
    tickers: list[str],
    info: Callable[[str], dict],
    *,
    sleep_s: float = 1.0,
    existing: pd.DataFrame | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """One row per ticker; resumable: rows fetched within 7 days in ``existing`` are reused.

    A ticker whose fetch fails is logged and skipped (its stale row, if any, is kept) so one bad
    symbol never aborts a run of hundreds.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=PEER_REFRESH_DAYS)
    kept: dict[str, dict] = {}
    if existing is not None and not existing.empty:
        kept = {row["ticker"]: row for row in existing.to_dict("records")}
    rows = []
    for ticker in dict.fromkeys(tickers):
        old = kept.get(ticker)
        if old is not None and pd.Timestamp(old["fetched_at"]) >= cutoff:
            rows.append(old)
            continue
        try:
            rows.append(_row(ticker, info(ticker), now.isoformat(timespec="seconds")))
        except Exception as exc:  # yfinance raises many types; one bad ticker must not stop a run
            log.warning("peer table: skipped %s (%s: %s)", ticker, type(exc).__name__, exc)
            if old is not None:
                rows.append(old)
        sleep(sleep_s)
    return pd.DataFrame(rows, columns=list(COLUMNS))


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number > 0.0 else None


def _median(rows: list[dict], key: str) -> float | None:
    values = [v for v in (row[key] for row in rows) if v is not None]
    return statistics.median(values) if values else None


def _peer(row: dict) -> dict:
    return {
        "ticker": row["ticker"],
        "name": row["name"],
        "market_cap": _number(row["market_cap"]),
        **{key: _positive(row[key]) for key in _MULTIPLES},
    }


def _nearest(candidates: pd.DataFrame, market_cap: float) -> list[dict]:
    sized = candidates[candidates["market_cap"] > 0.0]
    distance = (sized["market_cap"].map(math.log) - math.log(market_cap)).abs()
    return sized.loc[distance.sort_values(kind="stable").index[:MAX_PEERS]].to_dict("records")


def peers_for(
    ticker: str,
    industry: str | None,
    sector: str | None,
    market_cap: float,
    table: pd.DataFrame,
    *,
    company_multiples: dict[str, float | None] | None = None,
) -> dict:
    """The ``multiples`` section: nearest peers by market cap, same industry else same sector.

    Medians skip nulls and non-positive multiples (a negative P/E is a loss, not a price).
    """
    others = table[table["ticker"] != ticker]
    pools = (
        ("industry", others[others["industry"] == industry] if industry else others.iloc[0:0]),
        ("sector", others[others["sector"] == sector] if sector else others.iloc[0:0]),
    )
    for basis, pool in pools:
        peers = _nearest(pool, market_cap) if market_cap > 0.0 else []
        if len(peers) >= MIN_PEERS:
            listed = [_peer(row) for row in peers]
            return {
                "status": "ok",
                "basis": basis,
                "company": {
                    "ticker": ticker,
                    "market_cap": market_cap,
                    **{key: (company_multiples or {}).get(key) for key in _MULTIPLES},
                },
                "peers": listed,
                "median": {key: _median(listed, key) for key in _MULTIPLES},
                "note": _NOTE,
            }
    return {
        "status": "unavailable",
        "reason": "fewer than four comparable companies in the peer table",
    }
