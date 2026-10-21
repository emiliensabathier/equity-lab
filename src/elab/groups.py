"""Groups split into their reported branches, and the companies and brands in each branch.

Yahoo has no segment data, so each group's branches are typed in by hand from its own annual
report, one JSON file per group in ``group_data/`` with the source documents listed. A file is
refused unless its branches and its "other" line add up to the reported totals, so a typo shows
up as a failed load (and a failed test), not as a wrong figure on the page.

A branch's own figures are all a company publishes below group level: brands inside a branch
have no public accounts, unless one is listed on its own, in which case it links to its analysis.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from elab.brands import normalize

GROUP_DIR = Path(__file__).parent / "group_data"
# Rounding: each published line may be off by up to one unit, so the sum by one per line.
_TOLERANCE_PER_LINE = 1.0


class GroupDataError(ValueError):
    """A group file that is malformed or does not add up."""


def branch_id(name: str) -> str:
    """URL-safe id of a branch: "Fashion & Leather Goods" -> "fashion-leather-goods"."""
    return normalize(name).replace(" ", "-")


def _series(values: object, years: int, where: str, nullable: bool) -> list[float | None]:
    if not isinstance(values, list) or len(values) != years:
        raise GroupDataError(f"{where}: expected {years} values")
    for v in values:
        if v is None and nullable:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise GroupDataError(f"{where}: {v!r} is not a number")
    return [None if v is None else float(v) for v in values]


def _check_sum(group: dict, key: str) -> None:
    """Branches plus "other" must give the total each year, within rounding."""
    lines = [*group["branches"], group["other"]]
    for i, total in enumerate(group["total"][key]):
        parts = [line[key][i] for line in lines]
        if total is None or any(p is None for p in parts):
            continue  # a profit not published for one branch cannot be reconciled
        tolerance = _TOLERANCE_PER_LINE * (len(parts) + 1)
        if abs(sum(parts) - total) > tolerance:
            raise GroupDataError(
                f"{group['ticker']} {key} {group['years'][i]}: lines add up to "
                f"{sum(parts):.0f}, total is {total:.0f}"
            )


def validate(raw: dict) -> dict:
    """A checked copy of one group file, with branch ids; raises GroupDataError."""
    try:
        years = list(raw["years"])
        n = len(years)
        if n < 2 or len(raw["year_ends"]) != n:
            raise GroupDataError("at least two years, each with a year end")
        group = {
            "ticker": str(raw["ticker"]).upper(),
            "name": str(raw["name"]),
            "currency": str(raw["currency"]),
            "years": years,
            "year_ends": list(raw["year_ends"]),
            "profit_label": str(raw["profit_label"]),
            "segment_basis": str(raw.get("segment_basis") or "segments"),
            "sources": [{"title": str(s["title"]), "url": str(s["url"])} for s in raw["sources"]],
            "notes": str(raw.get("notes") or ""),
            "aliases": [str(t).upper() for t in raw.get("aliases", [])],
        }
        if not group["sources"]:
            raise GroupDataError("no source document")
        where = group["ticker"]
        group["total"] = {
            "revenue": _series(raw["total"]["revenue"], n, f"{where} total", False),
            "operating_profit": _series(
                raw["total"]["operating_profit"], n, f"{where} total", True
            ),
        }
        branches = []
        for b in raw["branches"]:
            name = str(b["name"])
            branches.append({
                "id": branch_id(name),
                "name": name,
                "revenue": _series(b["revenue"], n, f"{where} {name}", False),
                "operating_profit": _series(b["operating_profit"], n, f"{where} {name}", True),
                "brands": [str(x) for x in b.get("brands", [])],
                "listed": [
                    {"ticker": str(x["ticker"]).upper(), "name": str(x["name"])}
                    for x in b.get("listed", [])
                ],
                "comparables": [
                    {"ticker": str(x["ticker"]).upper(), "name": str(x["name"])}
                    for x in b.get("comparables", [])
                ],
            })
        if len(branches) < 2:
            raise GroupDataError(f"{where}: a group needs at least two branches")
        if len({b["id"] for b in branches}) != len(branches):
            raise GroupDataError(f"{where}: two branches share an id")
        group["branches"] = branches
        other = raw.get("other") or {"name": "Other", "revenue": [0] * n,
                                     "operating_profit": [0] * n}
        group["other"] = {
            "name": str(other["name"]),
            "revenue": _series(other["revenue"], n, f"{where} other", False),
            "operating_profit": _series(other["operating_profit"], n, f"{where} other", True),
        }
    except (KeyError, TypeError) as exc:
        raise GroupDataError(f"malformed group file: {exc!r}") from exc
    _check_sum(group, "revenue")
    _check_sum(group, "operating_profit")
    return group


def load_groups(directory: Path = GROUP_DIR) -> dict[str, dict]:
    """Checked groups by ticker (aliases included, e.g. GOOG for GOOGL)."""
    groups: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        group = validate(json.loads(path.read_text(encoding="utf-8")))
        for ticker in (group["ticker"], *group["aliases"]):
            groups[ticker] = group
    return groups


def find_branch(groups: dict[str, dict], ticker: str, brand: str) -> str:
    """Id of the branch of ``ticker``'s group that holds ``brand``, or "" when none does.

    The brand or branch name typed in full wins; otherwise the first branch with a brand that
    contains it as whole words ("Dior" -> "Christian Dior", in Fashion & Leather Goods).
    """
    group = groups.get(ticker)
    key = normalize(brand)
    if group is None or not key:
        return ""
    names = [
        (b["id"], [normalize(n) for n in (b["name"], *b["brands"])]) for b in group["branches"]
    ]
    for branch, keys in names:
        if key in keys:
            return branch
    for branch, keys in names:
        if any(f" {key} " in f" {k} " for k in keys):
            return branch
    return ""


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _growth(series: list[float | None]) -> float | None:
    """Latest year over the one before."""
    prev, last = series[-2], series[-1]
    if prev is None or last is None or prev <= 0:
        return None
    return last / prev - 1.0


def _cagr(series: list[float | None]) -> float | None:
    first, last = series[0], series[-1]
    if first is None or last is None or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / (len(series) - 1)) - 1.0


def _line(line: dict, total: dict) -> dict:
    revenue, profit = line["revenue"], line["operating_profit"]
    return {
        **line,
        "margin": [_ratio(p, r) for p, r in zip(profit, revenue, strict=True)],
        "growth": _growth(revenue),
        "cagr": _cagr(revenue),
        "revenue_share": _ratio(revenue[-1], total["revenue"][-1]),
        "profit_share": _ratio(profit[-1], total["operating_profit"][-1])
        if (profit[-1] or 0) > 0 and (total["operating_profit"][-1] or 0) > 0
        else None,
    }


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _comparable(ticker: str, name: str, table: pd.DataFrame | None) -> dict:
    """A listed comparable with its peer-table multiples, or bare when it is not in the table."""
    row = {"ticker": ticker, "name": name, "market_cap": None, "ev_ebitda": None,
           "ev_sales": None, "pe": None}
    if table is not None and not table.empty:
        hit = table[table["ticker"] == ticker]
        if not hit.empty:
            record = hit.iloc[0].to_dict()
            for key in ("market_cap", "ev_ebitda", "ev_sales", "pe"):
                row[key] = _number(record.get(key))
    return row


def group_view(group: dict, table: pd.DataFrame | None = None) -> dict:
    """The /api/group payload: every branch with its margins, growth and weight in the group."""
    total = group["total"]
    branches = []
    for b in group["branches"]:
        # A brand listed on its own carries its ticker; a listed member not named as a brand
        # (TRATON in a trucks branch) is added after the brands.
        listed = {normalize(x["name"]): x for x in b["listed"]}
        line = _line(b, total)
        line["brands"] = [
            {"name": name, "ticker": listed.pop(normalize(name), {}).get("ticker")}
            for name in b["brands"]
        ] + [{"name": x["name"], "ticker": x["ticker"]} for x in listed.values()]
        line["comparables"] = [_comparable(c["ticker"], c["name"], table)
                               for c in b["comparables"]]
        del line["listed"]
        branches.append(line)
    return {
        "ticker": group["ticker"],
        "name": group["name"],
        "currency": group["currency"],
        "years": group["years"],
        "year_ends": group["year_ends"],
        "profit_label": group["profit_label"],
        "segment_basis": group["segment_basis"],
        "sources": group["sources"],
        "notes": group["notes"],
        "total": {**total, "margin": [_ratio(p, r) for p, r in
                                      zip(total["operating_profit"], total["revenue"],
                                          strict=True)],
                  "growth": _growth(total["revenue"]), "cagr": _cagr(total["revenue"])},
        "branches": branches,
        "other": _line(group["other"], total),
    }


def peer_table_reader(cache_dir: Path, file_name: str) -> Callable[[], pd.DataFrame | None]:
    """Reader of the peer table, None when the weekly job has not written it yet."""

    def read() -> pd.DataFrame | None:
        path = cache_dir / file_name
        if not path.is_file():
            return None
        return pd.read_parquet(path)

    return read


def comparable_tickers(directory: Path = GROUP_DIR) -> list[str]:
    """Every branch comparable, for the weekly peer-table job; sorted, no duplicates."""
    return sorted({
        c["ticker"]
        for group in load_groups(directory).values()
        for b in group["branches"]
        for c in b["comparables"]
    })
