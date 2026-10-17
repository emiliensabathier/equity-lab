"""Command line: ``serve`` the site, ``build-peers`` peer table, ``pregenerate`` popular pages."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from elab import config
from elab.api import normalize_ticker

log = logging.getLogger("elab")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8040


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elab", description="equity-lab jobs and server")
    sub = parser.add_subparsers(dest="command", required=True)
    data_default = Path(os.environ.get("EQUITY_LAB_DATA", "data"))
    for name, text in (
        ("serve", "run the web server (single worker)"),
        ("build-peers", "rebuild the peer table from all index constituents"),
        ("pregenerate", "analyze the popular tickers and generate their narratives"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--data-dir", type=Path, default=data_default)
        if name == "serve":
            cmd.add_argument("--host", default=DEFAULT_HOST)
            cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def pregenerate(
    tickers: Sequence[str],
    *,
    analyze: Callable[[str], dict],
    cache,
    worker,
) -> tuple[int, int]:
    """Analyze each ticker, queue its narrative and wait for it before the next one.

    The analysis is stored under the key the API reads, so pages are instant. A failing ticker
    is logged and skipped. Returns (ok, failed).
    """
    ok = failed = 0
    for raw in tickers:
        try:
            ticker = normalize_ticker(raw)
            result = analyze(ticker)
            cache.set(f"analysis:{ticker}", result)
            worker.request(result)
            worker.drain()
            ok += 1
            log.info("pregenerated %s", ticker)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the run
            failed += 1
            log.warning("pregenerate: %s failed (%s: %s)", raw, type(exc).__name__, exc)
    log.info("pregenerate done: %d ok, %d failed", ok, failed)
    return ok, failed


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from elab.api import create_app
    from elab.cache import Cache
    from elab.engine import default_fetchers
    from elab.narrative import ClaudeRunner, NarrativeWorker
    from elab.resolve import _yahoo_search, local_peer_names

    args.data_dir.mkdir(parents=True, exist_ok=True)
    cache = Cache(args.data_dir / "cache.sqlite")
    app = create_app(
        fetchers=default_fetchers(), cache=cache,
        worker=NarrativeWorker(cache, ClaudeRunner(args.data_dir)),
        searcher=_yahoo_search, local_names=local_peer_names(args.data_dir),
        cache_dir=args.data_dir, static_dir=REPO_ROOT / "static",
    )
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
    return 0


def _cmd_build_peers(args: argparse.Namespace) -> int:
    import pandas as pd

    from elab.engine import default_fetchers
    from elab.peers import build_peer_table, constituents

    args.data_dir.mkdir(parents=True, exist_ok=True)
    path = args.data_dir / config.PEER_TABLE_FILE
    tickers: list[str] = []
    for source in config.CONSTITUENT_SOURCES:
        try:
            tickers.extend(constituents(source))
        except Exception as exc:  # noqa: BLE001 - one index page failing must not stop the rest
            log.warning("constituents: %s skipped (%s: %s)", source.index, type(exc).__name__, exc)
    if not tickers:
        log.error("no constituents fetched; peer table left untouched")
        return 1
    existing = pd.read_parquet(path) if path.is_file() else None
    table = build_peer_table(tickers, default_fetchers().info, existing=existing)
    # The server reads the table while this job rewrites it: write aside, then swap atomically.
    partial = path.with_name(path.name + ".tmp")
    try:
        table.to_parquet(partial)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    log.info("peer table: %d rows -> %s", len(table), path)
    return 0


def _cmd_pregenerate(args: argparse.Namespace) -> int:
    from elab.cache import Cache
    from elab.engine import analyze, default_fetchers
    from elab.narrative import ClaudeRunner, NarrativeWorker

    args.data_dir.mkdir(parents=True, exist_ok=True)
    cache = Cache(args.data_dir / "cache.sqlite")
    fetchers = default_fetchers()
    ok, _ = pregenerate(
        config.PREGENERATE,
        analyze=lambda t: analyze(t, fetchers=fetchers, cache_dir=args.data_dir),
        cache=cache,
        worker=NarrativeWorker(cache, ClaudeRunner(args.data_dir)),
    )
    return 0 if ok else 1


COMMANDS = {"serve": _cmd_serve, "build-peers": _cmd_build_peers, "pregenerate": _cmd_pregenerate}


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
