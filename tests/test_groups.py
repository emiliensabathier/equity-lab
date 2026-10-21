"""Group files: loading, the adding-up check, and the branch figures served to the page."""

from __future__ import annotations

import copy
import json

import pandas as pd
import pytest

from elab.groups import (
    GROUP_DIR,
    GroupDataError,
    branch_id,
    comparable_tickers,
    find_branch,
    group_view,
    load_groups,
    validate,
)

RAW = {
    "ticker": "mc.pa", "name": "Luxe", "currency": "EUR",
    "years": ["FY2024", "FY2025"], "year_ends": ["2024-12-31", "2025-12-31"],
    "profit_label": "Profit from recurring operations", "segment_basis": "business groups",
    "sources": [{"title": "Annual report", "url": "https://example.com/ar.pdf"}],
    "total": {"revenue": [100, 110], "operating_profit": [20, 22]},
    "branches": [
        {"name": "Fashion & Leather", "revenue": [60, 70], "operating_profit": [18, 21],
         "brands": ["Louis Vuitton", "Porsche"],
         "listed": [{"ticker": "p911.de", "name": "Porsche"},
                    {"ticker": "8tra.de", "name": "TRATON"}],
         "comparables": [{"ticker": "KER.PA", "name": "Kering"},
                         {"ticker": "ZZZ", "name": "Not in table"}]},
        {"name": "Wines", "revenue": [45, 45], "operating_profit": [5, None], "brands": ["Moët"]},
    ],
    "other": {"name": "Other and eliminations", "revenue": [-5, -5],
              "operating_profit": [-3, -3]},
}


def test_branch_ids_are_url_safe():
    assert branch_id("Fashion & Leather Goods") == "fashion-leather-goods"
    assert branch_id("Moët Hennessy") == "moet-hennessy"


def test_validate_upper_cases_tickers_and_gives_branch_ids():
    group = validate(RAW)
    assert group["ticker"] == "MC.PA"
    assert [b["id"] for b in group["branches"]] == ["fashion-leather", "wines"]
    assert group["branches"][0]["listed"][0]["ticker"] == "P911.DE"


def test_a_group_whose_lines_do_not_add_up_is_refused():
    raw = copy.deepcopy(RAW)
    raw["branches"][0]["revenue"] = [60, 80]
    with pytest.raises(GroupDataError, match="revenue FY2025"):
        validate(raw)


def test_rounding_within_one_unit_per_line_is_accepted():
    raw = copy.deepcopy(RAW)
    raw["total"]["revenue"] = [102, 108]
    assert validate(raw)["total"]["revenue"] == [102.0, 108.0]


def test_an_unpublished_branch_profit_skips_that_year_check():
    raw = copy.deepcopy(RAW)
    raw["total"]["operating_profit"] = [20, 999]  # FY2025 Wines profit is null
    validate(raw)


@pytest.mark.parametrize("patch", [
    {"years": ["FY2025"], "year_ends": ["2025-12-31"]},
    {"sources": []},
    {"branches": RAW["branches"][:1]},
    {"total": {"revenue": [100], "operating_profit": [20, 22]}},
    {"total": {"revenue": [100, "x"], "operating_profit": [20, 22]}},
    {"total": {"revenue": [100, None], "operating_profit": [20, 22]}},
])
def test_malformed_groups_are_refused(patch):
    with pytest.raises(GroupDataError):
        validate({**copy.deepcopy(RAW), **patch})


def test_missing_keys_are_a_group_data_error():
    raw = copy.deepcopy(RAW)
    del raw["profit_label"]
    with pytest.raises(GroupDataError):
        validate(raw)


def test_group_view_computes_margins_growth_and_weights():
    view = group_view(validate(RAW))
    fashion, wines = view["branches"]
    assert fashion["margin"] == [pytest.approx(0.3), pytest.approx(0.3)]
    assert fashion["growth"] == pytest.approx(70 / 60 - 1)
    assert fashion["revenue_share"] == pytest.approx(70 / 110)
    assert fashion["profit_share"] == pytest.approx(21 / 22)
    assert wines["margin"][1] is None and wines["profit_share"] is None
    assert view["total"]["margin"] == [pytest.approx(0.2), pytest.approx(0.2)]
    assert view["other"]["revenue_share"] == pytest.approx(-5 / 110)


def test_group_view_links_listed_members_and_keeps_unlisted_brands_bare():
    fashion = group_view(validate(RAW))["branches"][0]
    assert fashion["brands"] == [
        {"name": "Louis Vuitton", "ticker": None},
        {"name": "Porsche", "ticker": "P911.DE"},
        {"name": "TRATON", "ticker": "8TRA.DE"},
    ]
    assert "listed" not in fashion


def test_comparables_take_multiples_from_the_peer_table_when_there():
    table = pd.DataFrame([{"ticker": "KER.PA", "name": "Kering SA", "market_cap": 3e10,
                           "ev_ebitda": 9.5, "ev_sales": 2.0, "pe": -4.0}])
    kering, missing = group_view(validate(RAW), table)["branches"][0]["comparables"]
    assert kering == {"ticker": "KER.PA", "name": "Kering", "market_cap": 3e10,
                      "ev_ebitda": 9.5, "ev_sales": 2.0, "pe": None}  # a loss is not a P/E
    assert missing["ev_ebitda"] is None and missing["name"] == "Not in table"


def test_load_groups_indexes_aliases(tmp_path):
    raw = {**copy.deepcopy(RAW), "aliases": ["goog"]}
    (tmp_path / "x.json").write_text(json.dumps(raw))
    groups = load_groups(tmp_path)
    assert set(groups) == {"MC.PA", "GOOG"}
    assert find_branch(groups, "MC.PA", "Moët") == "wines"
    assert find_branch(groups, "MC.PA", "fashion & leather") == "fashion-leather"


def test_find_branch_matches_whole_words_after_exact_names():
    groups = {"MC.PA": validate(RAW)}
    assert find_branch(groups, "MC.PA", "Vuitton") == "fashion-leather"
    assert find_branch(groups, "MC.PA", "Vuit") == ""  # part of a word is not a match
    assert find_branch(groups, "MC.PA", "Gucci") == ""
    assert find_branch(groups, "KER.PA", "Gucci") == ""


def test_every_shipped_group_file_loads_and_adds_up():
    groups = load_groups(GROUP_DIR)
    for group in groups.values():
        assert group["sources"] and len(group["branches"]) >= 2


def test_comparable_tickers_are_listed_once(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps(RAW))
    assert comparable_tickers(tmp_path) == ["KER.PA", "ZZZ"]
