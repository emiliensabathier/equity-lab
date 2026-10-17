"""Turn a typed company name into tickers, and a Yahoo info dict into a described company.

Trading and reporting currency are kept apart on purpose: an ADR trades in USD but reports in
EUR, and a London listing quotes in pence while reporting in pounds. Mixing them silently would
misstate every per-share figure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vlab.errors import DataError
from vlab.universe import Company

from elab.config import (
    EXCLUDED_SECTORS,
    FALLBACK_INDEX,
    INDEX_BY_SUFFIX,
    PEER_TABLE_FILE,
    US_EXCHANGES,
)

MAX_MATCHES = 8
MIN_QUERY, MAX_QUERY = 2, 60
PENCE_CODES = frozenset({"GBp", "GBX"})
SUBUNIT_CODES = frozenset({"ZAc", "ILA"})


class UnknownTicker(DataError):
    """The symbol is not a listed equity Yahoo knows; the API maps this to a 404."""


@dataclass(frozen=True)
class Match:
    ticker: str
    name: str
    exchange: str


@dataclass(frozen=True)
class CompanyMeta:
    company: Company
    exchange: str
    sector: str
    industry: str
    market_index: str
    price_scale: float
    excluded_reason: str | None


def _yahoo_search(query: str) -> list[dict]:
    # Imported lazily so importing this module never touches the network stack.
    import yfinance as yf

    return list(yf.Search(query, max_results=20).quotes)


def _suffix(ticker: str) -> str:
    return ticker[ticker.rindex("."):] if "." in ticker else ""


def local_peer_names(cache_dir: Path) -> Callable[[], list[tuple[str, str]]]:
    """Reader of (ticker, name) rows from the peer table; no file means no rows."""

    def read() -> list[tuple[str, str]]:
        import pandas as pd

        path = cache_dir / PEER_TABLE_FILE
        if not path.is_file():
            return []
        table = pd.read_parquet(path, columns=["ticker", "name"])
        names = table["name"].fillna("").astype(str)
        return list(zip(table["ticker"].astype(str), names, strict=True))

    return read


def _local_matches(query: str, rows: list[tuple[str, str]]) -> list[Match]:
    """Rows where the query is a prefix of the ticker or of any word of the name."""
    needle = query.lower()
    return [
        Match(ticker, name, "")
        for ticker, name in rows
        if ticker.lower().startswith(needle)
        or any(word.startswith(needle) for word in name.lower().split())
    ]


def search(
    query: str,
    searcher: Callable[[str], list[dict]] = _yahoo_search,
    local_names: Callable[[], list[tuple[str, str]]] | None = None,
) -> list[Match]:
    """Listed US or covered-European equities matching a free-text query, at most 8.

    Yahoo returns few quotes and often only ETFs, so when it yields fewer than 8 covered
    equities the peer table fills the rest (Yahoo results first).
    """
    query = query.strip()
    if not MIN_QUERY <= len(query) <= MAX_QUERY:
        return []
    matches: list[Match] = []
    for quote in searcher(query):
        symbol = quote.get("symbol") or ""
        exchange = quote.get("exchange") or ""
        if quote.get("quoteType") != "EQUITY" or not symbol:
            continue
        if exchange not in US_EXCHANGES and (_suffix(symbol) or None) not in INDEX_BY_SUFFIX:
            continue
        name = quote.get("longname") or quote.get("shortname") or symbol
        matches.append(Match(symbol, name, exchange))
        if len(matches) == MAX_MATCHES:
            break
    if len(matches) < MAX_MATCHES and local_names is not None:
        seen = {m.ticker for m in matches}
        extra = [m for m in _local_matches(query, local_names()) if m.ticker not in seen]
        matches.extend(extra[: MAX_MATCHES - len(matches)])
    return matches


def company_from_info(ticker: str, info: dict) -> CompanyMeta:
    """Describe a company from Yahoo's info; refuse anything that is not an equity."""
    if info.get("quoteType") != "EQUITY":
        raise UnknownTicker(f"{ticker} is not a listed equity")
    trading = info.get("currency") or ""
    if not trading:
        raise UnknownTicker(f"{ticker} has no trading currency")
    reporting = info.get("financialCurrency") or trading
    if {trading, reporting} & SUBUNIT_CODES:
        raise UnknownTicker(f"{ticker} is quoted in a sub-unit currency we do not convert")
    price_scale = 1.0
    if trading in PENCE_CODES:
        trading, price_scale = "GBP", 0.01
    if reporting in PENCE_CODES:
        reporting = "GBP"
    exchange = info.get("exchange") or ""
    # A US exchange wins over dots in the symbol (BRK.B, BRK-B); otherwise the last suffix.
    market_index = (
        INDEX_BY_SUFFIX[""]
        if exchange in US_EXCHANGES
        else INDEX_BY_SUFFIX.get(_suffix(ticker) or None, FALLBACK_INDEX)
    )
    sector = info.get("sector") or ""
    excluded = (
        f"{sector} companies are not covered: a free-cash-flow DCF does not apply "
        "to balance-sheet businesses."
        if sector in EXCLUDED_SECTORS
        else None
    )
    company = Company(
        ticker, info.get("longName") or info.get("shortName") or ticker, reporting, trading
    )
    return CompanyMeta(
        company=company,
        exchange=exchange,
        sector=sector,
        industry=info.get("industry") or "",
        market_index=market_index,
        price_scale=price_scale,
        excluded_reason=excluded,
    )
