import numpy as np
import pandas as pd
import pytest
from vlab.fundamentals import drivers_from

from elab.config import DEFAULTABLE_LINES
from elab.statements import MissingCoreLine, fill_defaults, limit_sentence
from tests.conftest import make_statements


def test_fill_defaults_adds_zero_rows_for_missing_defaultable_lines():
    s = make_statements()
    s = make_statements(
        cashflow=s.cashflow.drop(index="Sale Of PPE"),
        balance=s.balance.drop(index="Inventory"),
    )
    filled, names = fill_defaults(s)
    assert names == ("Sale Of PPE", "Inventory")
    assert (filled.cashflow.loc["Sale Of PPE"] == 0.0).all()
    assert (filled.balance.loc["Inventory"] == 0.0).all()


def test_fill_defaults_leaves_input_unmodified():
    s = make_statements()
    s = make_statements(balance=s.balance.drop(index="Inventory"))
    before = s.balance.copy()
    fill_defaults(s)
    pd.testing.assert_frame_equal(s.balance, before)
    assert "Inventory" not in s.balance.index


def test_fill_defaults_does_not_share_info_dict():
    s = make_statements()
    filled, _ = fill_defaults(s)
    assert filled.info == s.info
    assert filled.info is not s.info


def test_fill_defaults_raises_on_missing_core_line():
    s = make_statements()
    s = make_statements(income=s.income.drop(index="EBIT"))
    with pytest.raises(MissingCoreLine) as err:
        fill_defaults(s)
    assert err.value.line == "EBIT"


def test_fill_defaults_raises_on_all_nan_core_line():
    income = make_statements().income.copy()
    income.loc["EBIT"] = np.nan
    with pytest.raises(MissingCoreLine) as err:
        fill_defaults(make_statements(income=income))
    assert err.value.line == "EBIT"


def test_fill_defaults_treats_all_nan_row_as_missing():
    balance = make_statements().balance.copy()
    balance.loc["Minority Interest"] = np.nan
    filled, names = fill_defaults(make_statements(balance=balance))
    assert names == ("Minority Interest",)
    assert (filled.balance.loc["Minority Interest"] == 0.0).all()


def test_fill_defaults_reports_nothing_when_complete():
    filled, names = fill_defaults(make_statements())
    assert names == ()
    assert filled.income.equals(make_statements().income)


def test_limit_sentence():
    assert limit_sentence("Inventory") == "Inventory not reported: treated as zero."


def test_filled_statements_pass_vlab_drivers_from():
    s = make_statements()
    defaultable = {line for lines in DEFAULTABLE_LINES.values() for line in lines}
    s = make_statements(
        income=s.income.drop(index=list(defaultable & set(s.income.index))),
        cashflow=s.cashflow.drop(index=list(defaultable & set(s.cashflow.index))),
        balance=s.balance.drop(index=list(defaultable & set(s.balance.index))),
    )
    filled, names = fill_defaults(s)
    assert len(names) == 8
    assert drivers_from(filled, "X").lease_ratio == 0.0
