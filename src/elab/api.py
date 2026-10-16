"""HTTP layer: search, cached analysis, narrative polling, per-IP limits.

The rule here is that a recruiter never sees a 500. Yahoo being down degrades to the last good
copy (flagged stale) or a plain 503; only an unknown ticker is a 404, and exception text is
logged, never returned.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from elab import config
from elab.cache import Cache
from elab.engine import Fetchers, analyze
from elab.narrative import NarrativeWorker
from elab.resolve import UnknownTicker, search

log = logging.getLogger(__name__)

RATE_LIMIT_MESSAGE = "Too many new analyses from this address; cached companies stay available."
UNAVAILABLE_MESSAGE = "Data source unavailable, try again in a minute."
LOOPBACK = frozenset({"127.0.0.1", "::1"})
_TICKER = re.compile(config.TICKER_PATTERN)


class SlidingWindowLimiter:
    """Thread-safe in-memory budget per key over a sliding window; the clock is injectable."""

    def __init__(
        self, limit: int, window: timedelta, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window.total_seconds()
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> bool:
        """Record a hit and return True, or return False (recording nothing) when over budget."""
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


def client_ip(request: Request) -> str:
    """Peer address; X-Forwarded-For's first hop only when the peer is our local proxy."""
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if peer in LOOPBACK and forwarded:
        return forwarded.split(",")[0].strip() or peer
    return peer


def create_app(
    *,
    fetchers: Fetchers,
    cache: Cache,
    worker: NarrativeWorker,
    searcher: Callable[[str], list[dict]],
    cache_dir: Path,
    static_dir: Path,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    app = FastAPI(title="equity-lab", docs_url=None, redoc_url=None)
    cold_limiter = SlidingWindowLimiter(config.COLD_ANALYSES_PER_HOUR, config.RATE_WINDOW, clock)
    narrative_limiter = SlidingWindowLimiter(
        config.NARRATIVE_JOBS_PER_HOUR, config.RATE_WINDOW, clock
    )
    ticker_locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    def lock_for(ticker: str) -> threading.Lock:
        with locks_guard:
            return ticker_locks.setdefault(ticker, threading.Lock())

    def normalize(raw: str) -> str:
        ticker = raw.upper()
        if not _TICKER.match(ticker):
            raise HTTPException(422, "Invalid ticker.")
        return ticker

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error on %s", request.url.path, exc_info=exc)
        return JSONResponse({"detail": UNAVAILABLE_MESSAGE}, status_code=503)

    @app.get("/api/search")
    def api_search(q: str = "") -> list[dict]:
        try:
            return [asdict(m) for m in search(q, searcher)]
        except Exception:
            log.exception("search failed")
            raise HTTPException(503, "Search is unavailable, try again in a minute.") from None

    @app.get("/api/analysis/{ticker}")
    def api_analysis(ticker: str, request: Request) -> dict:
        ticker = normalize(ticker)
        key = f"analysis:{ticker}"
        fresh = cache.get(key, config.ANALYSIS_TTL)
        if fresh is not None:
            return {**fresh[0], "stale": False}
        with lock_for(ticker):
            # A request that waited here is served by the analysis the first one just stored.
            fresh = cache.get(key, config.ANALYSIS_TTL)
            if fresh is not None:
                return {**fresh[0], "stale": False}
            if not cold_limiter.try_acquire(client_ip(request)):
                raise HTTPException(429, RATE_LIMIT_MESSAGE)
            try:
                result = analyze(ticker, fetchers=fetchers, cache_dir=cache_dir)
                cache.set(key, result)
            except UnknownTicker:
                raise HTTPException(404, "Unknown ticker.") from None
            except Exception:
                log.exception("analysis failed for %s", ticker)
                stale = cache.get(key, None)
                if stale is None:
                    raise HTTPException(503, UNAVAILABLE_MESSAGE) from None
                return {**stale[0], "stale": True}
            return {**result, "stale": False}

    @app.get("/api/analysis/{ticker}/narrative")
    def api_narrative(ticker: str, request: Request) -> dict:
        ticker = normalize(ticker)
        cached = cache.get(f"analysis:{ticker}", None)
        if cached is None:
            raise HTTPException(404, "No analysis for this ticker yet.")
        analysis = cached[0]
        if worker.get(analysis) is None and not worker.is_inflight(analysis):
            # Only a brand-new job costs a Claude call; polling a pending one is free.
            if not narrative_limiter.try_acquire(client_ip(request)):
                raise HTTPException(429, RATE_LIMIT_MESSAGE)
        status = worker.request(analysis)
        if status == "ready":
            ready = worker.get(analysis) or {}
            return {"status": "ready", "text": ready.get("text", "")}
        return {"status": status}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        page = Path(static_dir) / "index.html"
        if not page.is_file():
            raise HTTPException(404, "Page not built yet.")
        return FileResponse(page)

    app.mount("/static", StaticFiles(directory=static_dir, check_dir=False), name="static")
    return app
