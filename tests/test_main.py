"""CLI wiring and the pregenerate loop, with fakes: no network, no real claude."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_pregenerate_normalizes_tickers_like_the_api():
    events: list[tuple] = []
    cache = _Cache()
    ok, failed = pregenerate(
        ["aapl", "bad ticker!"],
        analyze=lambda t: events.append(("analyze", t)) or {"ticker": t},
        cache=cache,
        worker=_Worker(events),
    )
    assert (ok, failed) == (1, 1)
    assert ("analyze", "AAPL") in events
    assert set(cache.stored) == {"analysis:AAPL"}


def test_build_peers_command_smoke(tmp_path, monkeypatch):
    import pandas as pd

    from elab import __main__ as cli
    from elab import engine, peers

    monkeypatch.setattr(config, "CONSTITUENT_SOURCES", config.CONSTITUENT_SOURCES[:1])
    monkeypatch.setattr(peers, "constituents", lambda source: ["AAA", "BBB"])
    monkeypatch.setattr(engine, "default_fetchers", lambda: SimpleNamespace(info=lambda t: {}))
    seen = {}

    def fake_build(tickers, info, existing=None):
        seen["tickers"] = tickers
        return pd.DataFrame({"ticker": tickers, "name": tickers})

    monkeypatch.setattr(peers, "build_peer_table", fake_build)
    assert cli.main(["build-peers", "--data-dir", str(tmp_path)]) == 0
    assert seen["tickers"] == ["AAA", "BBB"]
    assert (tmp_path / config.PEER_TABLE_FILE).is_file()


def test_a_peer_table_write_that_fails_midway_leaves_the_previous_table(tmp_path, monkeypatch):
    # The server reads the table while the weekly job rewrites it: a torn file must never show.
    import pandas as pd

    from elab import __main__ as cli
    from elab import engine, peers

    previous = pd.DataFrame({"ticker": ["OLD"], "name": ["Old"]})
    previous.to_parquet(tmp_path / config.PEER_TABLE_FILE)
    monkeypatch.setattr(config, "CONSTITUENT_SOURCES", config.CONSTITUENT_SOURCES[:1])
    monkeypatch.setattr(peers, "constituents", lambda source: ["AAA"])
    monkeypatch.setattr(engine, "default_fetchers", lambda: SimpleNamespace(info=lambda t: {}))
    monkeypatch.setattr(peers, "build_peer_table", lambda tickers, info, existing=None: previous)

    def torn(self, path, *args, **kwargs):
        Path(path).write_bytes(b"half a file")
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", torn)
    with pytest.raises(OSError):
        cli.main(["build-peers", "--data-dir", str(tmp_path)])
    monkeypatch.undo()

    assert pd.read_parquet(tmp_path / config.PEER_TABLE_FILE).equals(previous)
    assert [p.name for p in tmp_path.iterdir()] == [config.PEER_TABLE_FILE]


def test_build_peers_command_fails_without_constituents(tmp_path, monkeypatch):
    from elab import __main__ as cli
    from elab import peers

    def boom(source):
        raise RuntimeError("down")

    monkeypatch.setattr(peers, "constituents", boom)
    assert cli.main(["build-peers", "--data-dir", str(tmp_path)]) == 1
    assert not (tmp_path / config.PEER_TABLE_FILE).exists()


def test_pregenerate_command_smoke(tmp_path, monkeypatch):
    from elab import __main__ as cli
    from elab import engine, narrative

    monkeypatch.setattr(config, "PREGENERATE", ("aapl",))
    monkeypatch.setattr(engine, "default_fetchers", lambda: object())
    monkeypatch.setattr(
        engine, "analyze", lambda ticker, fetchers, cache_dir: {"ticker": ticker, "price": 1.0}
    )
    monkeypatch.setattr(narrative, "ClaudeRunner", lambda _: (lambda prompt: "Price is 1.0."))
    assert cli.main(["pregenerate", "--data-dir", str(tmp_path)]) == 0
    from elab.cache import Cache

    assert Cache(tmp_path / "cache.sqlite").get("analysis:AAPL", None) is not None


def test_serve_command_passes_args_to_uvicorn(tmp_path, monkeypatch):
    import uvicorn

    from elab import __main__ as cli
    from elab import engine, narrative

    monkeypatch.setattr(engine, "default_fetchers", lambda: object())
    monkeypatch.setattr(narrative, "ClaudeRunner", lambda _: (lambda prompt: ""))
    calls = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    rc = cli.main(["serve", "--host", "0.0.0.0", "--port", "9001", "--data-dir", str(tmp_path)])
    assert rc == 0
    assert (calls["host"], calls["port"], calls["workers"]) == ("0.0.0.0", 9001, 1)
    assert calls["app"].title == "equity-lab"
