import numpy as np
import pandas as pd
import pytest
from vlab.fundamentals import drivers_from

from elab.config import DEFAULTABLE_LINES
from elab.statements import (
    MissingCoreLine,
    blank_years_sentence,
    carried_sentence,
    fill_blank_years,
    fill_defaults,
    limit_sentence,
)
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


def _blank_latest(frame: pd.DataFrame, *lines: str) -> pd.DataFrame:
    blanked = frame.copy()
    for line in lines:
        blanked.loc[line, blanked.columns[0]] = np.nan
    return blanked


def test_fill_blank_years_carries_the_prior_year_into_blank_interest_and_leases():
    # Apple's leases and interest are reported for older years and blank for the latest two.
    base = make_statements()
    balance = base.balance.copy()
    balance.loc["Capital Lease Obligations", balance.columns[:2]] = np.nan
    s = make_statements(
        income=_blank_latest(base.income, "Interest Expense"), balance=balance
    )
    filled, carried, zeroed = fill_blank_years(s)

    assert carried == (
        ("Interest Expense", (2025,), 2024),
        ("Capital Lease Obligations", (2024, 2025), 2023),
    )
    assert zeroed == ()
    assert filled.income.loc["Interest Expense"].iloc[0] == 10.0
    assert list(filled.balance.loc["Capital Lease Obligations"].iloc[:2]) == [75.0, 75.0]
    assert np.isnan(s.balance.loc["Capital Lease Obligations"].iloc[0])  # input untouched


def test_fill_blank_years_zeroes_other_defaultable_lines_and_older_blanks():
    base = make_statements()
    cashflow = _blank_latest(base.cashflow, "Sale Of PPE")
    balance = base.balance.copy()
    balance.loc["Current Capital Lease Obligation", balance.columns[-1]] = np.nan
    filled, carried, zeroed = fill_blank_years(make_statements(cashflow=cashflow,
                                                               balance=balance))

    assert carried == ()
    assert zeroed == ("Sale Of PPE", "Current Capital Lease Obligation")
    assert filled.cashflow.loc["Sale Of PPE"].iloc[0] == 0.0
    assert filled.balance.loc["Current Capital Lease Obligation"].iloc[-1] == 0.0


def test_fill_blank_years_leaves_core_lines_and_unreported_years_alone():
    base = make_statements()
    income = _blank_latest(base.income, "EBIT")
    extra = pd.Timestamp("2021-12-31")
    income[extra] = np.nan  # a phantom year with nothing reported in it
    s = make_statements(income=income)
    filled, carried, zeroed = fill_blank_years(s)

    assert carried == zeroed == ()
    assert np.isnan(filled.income.loc["EBIT"].iloc[0])
    assert filled.income[extra].isna().all()


def test_blank_years_sentence():
    assert blank_years_sentence("Interest Expense") == (
        "Interest Expense not reported for some years: treated as zero in those years."
    )


def test_carried_sentence_names_the_line_and_the_years():
    assert carried_sentence("Interest Expense", (2025,), 2024) == (
        "Interest Expense not reported for 2025: 2024 value used."
    )
    assert carried_sentence("Capital Lease Obligations", (2024, 2025), 2023) == (
        "Capital Lease Obligations not reported for 2024 to 2025: 2023 value used."
    )
