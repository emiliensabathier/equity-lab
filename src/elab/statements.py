"""Make reported statements acceptable to the vlab engine without hiding what was assumed."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
from vlab.data.loader import Statements
from vlab.errors import DataError

from elab.config import CORE_LINES, DEFAULTABLE_LINES

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
