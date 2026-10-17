"""Make reported statements acceptable to the vlab engine without hiding what was assumed."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
from vlab.data.loader import Statements
from vlab.errors import DataError

from elab.config import CARRIED_LINES, CORE_LINES, DEFAULTABLE_LINES

_FRAMES = ("income", "cashflow", "balance")


class MissingCoreLine(DataError):
    """A line the valuation cannot do without is absent."""

    def __init__(self, line: str) -> None:
        super().__init__(f"statement line {line!r} is not reported")
        self.line = line


def _is_missing(frame: pd.DataFrame, line: str) -> bool:
    return line not in frame.index or bool(frame.loc[line].isna().all())


def limit_sentence(line: str) -> str:
    """The sentence shown in the page's limits for a line defaulted to zero."""
    return f"{line} not reported: treated as zero."


def fill_defaults(s: Statements) -> tuple[Statements, tuple[str, ...]]:
    """Return statements with missing defaultable lines set to zero, plus the lines filled.

    Inputs are never modified. A core line that is absent or all-NaN raises ``MissingCoreLine``; an
    all-NaN defaultable row counts as absent, since vlab would otherwise drop every year.
    """
    for name in _FRAMES:
        for line in CORE_LINES[name]:
            if _is_missing(getattr(s, name), line):
                raise MissingCoreLine(line)

    frames: dict[str, pd.DataFrame] = {}
    filled: list[str] = []
    for name in _FRAMES:
        frame = getattr(s, name).copy()
        for line in DEFAULTABLE_LINES[name]:
            if not _is_missing(frame, line):
                continue
            zero = pd.DataFrame(0.0, index=[line], columns=frame.columns)
            frame = pd.concat([frame.drop(index=line, errors="ignore"), zero])
            filled.append(line)
        frames[name] = frame
    return replace(s, **frames, info=dict(s.info)), tuple(filled)


def blank_years_sentence(line: str) -> str:
    """The sentence shown in the page's limits for a line zeroed in some reported years."""
    return f"{line} not reported for some years: treated as zero in those years."


def carried_sentence(line: str, years: tuple[int, ...], source: int) -> str:
    """The sentence shown in the page's limits for a line carried from an earlier year."""
    contiguous = list(years) == list(range(years[0], years[-1] + 1))
    span = (
        f"{years[0]}\u2013{years[-1]}" if contiguous and len(years) > 1
        else ", ".join(str(year) for year in years)
    )
    return f"{line} not reported for {span}: {source} value used."


Carried = tuple[str, tuple[int, ...], int]


def _fill_line(
    row: pd.Series, blank: pd.Series, carry: bool
) -> tuple[pd.Series, dict[int, list[int]], bool]:
    """One line with its blank cells filled; the years carried per source year; any zeroed."""
    values = row.copy()
    sources: dict[int, list[int]] = {}
    zeroed = False
    last: tuple[int, float] | None = None
    for column in sorted(row.index):  # oldest first, so "last" is the latest earlier value
        year = pd.Timestamp(column).year
        if not blank[column]:
            if row[column] == row[column]:  # not NaN
                last = (year, float(row[column]))
            continue
        if carry and last is not None:
            values[column] = last[1]
            sources.setdefault(last[0], []).append(year)
        else:
            values[column] = 0.0
            zeroed = True
    return values, sources, zeroed


def fill_blank_years(
    s: Statements,
) -> tuple[Statements, tuple[Carried, ...], tuple[str, ...]]:
    """Fill blank cells of defaultable lines in reported years; say which and how.

    A source can keep a line for older years and leave the latest ones empty (Apple's leases
    and interest). Interest and lease lines take the most recent earlier reported value, since
    zero would show a 0% cost of debt (or no leases) that the earlier years contradict; those
    are returned as (line, years, source year). Other defaultable lines, and blanks with no
    earlier value, are zeroed and returned by name. Only years where every core line of that
    statement is reported are filled, so a year the company did not report stays blank. Core
    lines are never touched, and inputs are never modified.
    """
    frames: dict[str, pd.DataFrame] = {}
    carried: list[Carried] = []
    zeroed: list[str] = []
    for name in _FRAMES:
        frame = getattr(s, name).copy()
        core = [line for line in CORE_LINES[name] if line in frame.index]
        reported = frame.loc[core].notna().all() if core else pd.Series(True, frame.columns)
        for line in DEFAULTABLE_LINES[name]:
            if line not in frame.index:
                continue
            blank = frame.loc[line].isna() & reported
            if not blank.any():
                continue
            values, sources, any_zeroed = _fill_line(
                frame.loc[line], blank, line in CARRIED_LINES
            )
            frame.loc[line] = values
            carried.extend((line, tuple(years), source) for source, years in sources.items())
            if any_zeroed:
                zeroed.append(line)
        frames[name] = frame
    return replace(s, **frames, info=dict(s.info)), tuple(carried), tuple(zeroed)
