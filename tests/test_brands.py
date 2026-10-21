"""Brand -> owner table: matching rules and the table's own consistency."""

from __future__ import annotations

import re

from elab.brands import BRAND_INDEX, OWNERS, brand_matches, normalize
from elab.config import TICKER_PATTERN


def test_normalize_drops_accents_case_and_punctuation():
    assert normalize("Moët & Chandon") == "moet chandon"
    assert normalize("L'Oréal") == "loreal"
    assert normalize("  Pull&Bear ") == "pull bear"


def test_zara_belongs_to_inditex():
    assert brand_matches("zara")[0] == ("ITX.MC", "Inditex", "Zara")
    assert brand_matches("ZARA ")[0][0] == "ITX.MC"


def test_accents_and_apostrophes_need_not_be_typed():
    assert brand_matches("lancome")[0][0] == "OR.PA"
    assert brand_matches("kiehls")[0][0] == "OR.PA"
    assert brand_matches("moet")[0][0] == "MC.PA"


def test_a_prefix_finds_brands_once_per_owner():
    rows = brand_matches("massim")
    assert rows == [("ITX.MC", "Inditex", "Massimo Dutti")]
    tickers = [t for t, _, _ in brand_matches("zar")]  # Zara and Zara Home: one row
    assert tickers == ["ITX.MC"]


def test_short_queries_match_only_whole_brands():
    assert brand_matches("vw")[0][0] == "VOW3.DE"
    assert brand_matches("gu") == []  # would begin Gucci and Guinness


def test_an_exact_brand_comes_before_longer_ones():
    assert brand_matches("dior")[0][2] == "Dior"
    assert brand_matches("mini")[0][0] == "BMW.DE"


def test_unknown_or_empty_queries_match_nothing():
    assert brand_matches("qwertyuiop") == []
    assert brand_matches("--") == []


def test_every_ticker_is_well_formed_and_every_brand_has_one_owner():
    pattern = re.compile(TICKER_PATTERN)
    assert all(pattern.fullmatch(t) for t, _, _ in OWNERS)
    assert len({t for t, _, _ in OWNERS}) == len(OWNERS)
    assert len(BRAND_INDEX) > 400
