"""CLI wiring and the pregenerate loop, with fakes: no network, no real claude."""

from __future__ import annotations

from pathlib import Path

import pytest

from elab import config
from elab.__main__ import build_parser, pregenerate


def test_cli_parses_subcommands(monkeypatch):
    monkeypatch.delenv("EQUITY_LAB_DATA", raising=False)
    parser = build_parser()
    serve = parser.parse_args(["serve"])
    assert (serve.command, serve.host, serve.port) == ("serve", "127.0.0.1", 8040)
    assert serve.data_dir == Path("data")
    custom = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000", "--data-dir", "x"])
    assert (custom.host, custom.port, custom.data_dir) == ("0.0.0.0", 9000, Path("x"))
    assert parser.parse_args(["build-peers"]).command == "build-peers"
    assert parser.parse_args(["pregenerate", "--data-dir", "d"]).data_dir == Path("d")
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["nope"])


def test_data_dir_defaults_to_env(monkeypatch):
    monkeypatch.setenv("EQUITY_LAB_DATA", "/srv/elab")
    assert build_parser().parse_args(["pregenerate"]).data_dir == Path("/srv/elab")


def test_pregenerate_list_is_sane():
    assert len(set(config.PREGENERATE)) == len(config.PREGENERATE) >= 40
    assert "MC.PA" in config.PREGENERATE and "AAPL" in config.PREGENERATE


class _Cache:
    def __init__(self):
        self.stored = {}

    def set(self, key, value):
        self.stored[key] = value


class _Worker:
    def __init__(self, events):
        self.events = events

    def request(self, analysis):
        self.events.append(("request", analysis["ticker"]))
        return "pending"

    def drain(self):
        self.events.append(("drain",))


def test_pregenerate_processes_list_one_at_a_time():
    events: list[tuple] = []

    def analyze(ticker):
        events.append(("analyze", ticker))
        if ticker == "BAD":
            raise RuntimeError("boom")
        return {"ticker": ticker}

    cache = _Cache()
    ok, failed = pregenerate(["A", "BAD", "B"], analyze=analyze, cache=cache,
                             worker=_Worker(events))
    assert (ok, failed) == (2, 1)
    assert events == [
        ("analyze", "A"), ("request", "A"), ("drain",),
        ("analyze", "BAD"),
        ("analyze", "B"), ("request", "B"), ("drain",),
    ]
    assert set(cache.stored) == {"analysis:A", "analysis:B"}
