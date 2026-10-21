"""HTTP layer: search, cached analysis, narrative polling, per-IP limits.

The rule here is that a recruiter never sees a 500. Yahoo being down degrades to the last good
copy (flagged stale) or a plain 503; only an unknown ticker is a 404, and exception text is
logged, never returned. An expired copy is served at once (flagged refreshing) while a
background thread recomputes it, so only a ticker never analysed makes a visitor wait.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from elab import config
from elab.brands import brand_matches
from elab.cache import Cache
from elab.engine import Fetchers, analyze
from elab.groups import find_branch, group_view, load_groups, peer_table_reader
from elab.narrative import NarrativeWorker
from elab.resolve import MAX_QUERY, MIN_QUERY, UnknownTicker, search

log = logging.getLogger(__name__)

RATE_LIMIT_MESSAGE = (
    "Too many new analyses from this address. Companies already analysed stay available."
)
SEARCH_RATE_MESSAGE = "Too many searches from this address. Try again in a minute."
UNAVAILABLE_MESSAGE = "Data source unavailable, try again in a minute."
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
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
            # Sweep every key so idle addresses do not accumulate for the life of the process.
            for k in list(self._hits):
                window = self._hits[k]
                while window and now - window[0] >= self._window:
                    window.popleft()
                if not window:
                    del self._hits[k]
            hits = self._hits[key]
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


class TtlCache:
    """Thread-safe in-memory map whose entries expire; the oldest go first past ``max_size``."""

    def __init__(self, ttl: timedelta, max_size: int, clock: Callable[[], float]) -> None:
        self._ttl = ttl.total_seconds()
        self._max_size = max_size
        self._clock = clock
        self._entries: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> list[dict] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or self._clock() - entry[0] >= self._ttl:
                return None
            return entry[1]

    def set(self, key: str, value: list[dict]) -> None:
        now = self._clock()
        with self._lock:
            self._entries = {
                k: v for k, v in self._entries.items() if now - v[0] < self._ttl and k != key
            }
            while len(self._entries) >= self._max_size:
                del self._entries[next(iter(self._entries))]
            self._entries[key] = (now, value)


def client_ip(request: Request) -> str:
    """Peer address; X-Forwarded-For's first hop only when the peer is our local proxy."""
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if peer in LOOPBACK and forwarded:
        return forwarded.split(",")[0].strip() or peer
    return peer


def normalize_ticker(raw: str) -> str:
    """Upper-case a ticker and reject anything outside the allowed pattern (HTTP 422)."""
    ticker = raw.upper()
    if not _TICKER.fullmatch(ticker):
        raise HTTPException(422, "Invalid ticker.")
    return ticker


def create_app(
    *,
    fetchers: Fetchers,
    cache: Cache,
    worker: NarrativeWorker,
    searcher: Callable[[str], list[dict]],
    local_names: Callable[[], list[tuple[str, str]]] = lambda: [],
    cache_dir: Path,
    static_dir: Path,
    clock: Callable[[], float] = time.monotonic,
    background: Callable[[Callable[[], None]], object] | None = None,
    groups: dict[str, dict] | None = None,
) -> FastAPI:
    app = FastAPI(title="equity-lab", docs_url=None, redoc_url=None)
    groups = load_groups() if groups is None else groups
    read_peer_table = peer_table_reader(cache_dir, config.PEER_TABLE_FILE)
    cold_limiter = SlidingWindowLimiter(config.COLD_ANALYSES_PER_HOUR, config.RATE_WINDOW, clock)
    narrative_limiter = SlidingWindowLimiter(
        config.NARRATIVE_JOBS_PER_HOUR, config.RATE_WINDOW, clock
    )
    search_limiter = SlidingWindowLimiter(config.SEARCHES_PER_MINUTE, config.SEARCH_WINDOW, clock)
    search_cache = TtlCache(config.SEARCH_CACHE_TTL, config.SEARCH_CACHE_MAX, clock)
    ticker_locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()
    # One refresh at a time: the engine serializes runs anyway, and Yahoo is not hammered.
    run_later = background or ThreadPoolExecutor(1, thread_name_prefix="refresh").submit
    refreshing: set[str] = set()

    def lock_for(ticker: str) -> threading.Lock:
        with locks_guard:
            return ticker_locks.setdefault(ticker, threading.Lock())

    normalize = normalize_ticker

    def refresh(ticker: str) -> None:
        key = f"analysis:{ticker}"
        try:
            with lock_for(ticker):
                if cache.get(key, config.ANALYSIS_TTL) is None:
                    cache.set(key, analyze(ticker, fetchers=fetchers, cache_dir=cache_dir))
        except Exception:
            log.exception("background refresh failed for %s", ticker)
        finally:
            with locks_guard:
                refreshing.discard(ticker)

    def schedule_refresh(ticker: str, ip: str) -> bool:
        """True once a refresh of ``ticker`` is queued or running; False when over budget."""
        with locks_guard:
            if ticker in refreshing:
                return True
            if not cold_limiter.try_acquire(ip):
                return False
            refreshing.add(ticker)
        try:
            run_later(lambda: refresh(ticker))
        except Exception:
            log.exception("could not queue a refresh for %s", ticker)
            with locks_guard:
                refreshing.discard(ticker)
            return False
        return True

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error on %s", request.url.path, exc_info=exc)
        return JSONResponse({"detail": UNAVAILABLE_MESSAGE}, status_code=503)

    @app.get("/api/search")
    def api_search(request: Request, q: str = "") -> list[dict]:
        if not search_limiter.try_acquire(client_ip(request)):
            raise HTTPException(429, SEARCH_RATE_MESSAGE)
        key = q.strip().lower()
        if not MIN_QUERY <= len(key) <= MAX_QUERY:
            return []
        cached = search_cache.get(key)
        if cached is not None:
            return cached
        try:
            found = [
                asdict(replace(m, branch=find_branch(groups, m.ticker, m.brand)))
                if m.brand else asdict(m)
                for m in search(q, searcher, local_names, brand_matches)
            ]
        except Exception:
            log.exception("search failed")
            raise HTTPException(503, "Search is unavailable, try again in a minute.") from None
        search_cache.set(key, found)
        return found

    @app.get("/api/group/{ticker}")
    def api_group(ticker: str) -> dict:
        group = groups.get(normalize_ticker(ticker))
        if group is None:
            raise HTTPException(404, "No branch breakdown for this company.")
        try:
            table = read_peer_table()
        except Exception:
            log.exception("peer table unreadable; comparables shown without multiples")
            table = None
        return group_view(group, table)

    @app.get("/api/analysis/{ticker}")
    def api_analysis(ticker: str, request: Request) -> dict:
        ticker = normalize(ticker)
        # Told on every answer, so the page asks for branches only when there are some.
        return {**analysis_for(ticker, request), "has_group": ticker in groups}

    def analysis_for(ticker: str, request: Request) -> dict:
        key = f"analysis:{ticker}"
        fresh = cache.get(key, config.ANALYSIS_TTL)
        if fresh is not None:
            return {**fresh[0], "stale": False}
        expired = cache.get(key, None)
        if expired is not None:
            # Served now; the visitor reloads for the new copy. Over budget, it stays as is.
            if schedule_refresh(ticker, client_ip(request)):
                return {**expired[0], "stale": False, "refreshing": True}
            return {**expired[0], "stale": True}
        with lock_for(ticker):
            # A request that waited here is served by the analysis the first one just stored.
            fresh = cache.get(key, config.ANALYSIS_TTL)
            if fresh is not None:
                return {**fresh[0], "stale": False}
            if not cold_limiter.try_acquire(client_ip(request)):
                stale = cache.get(key, None)
                if stale is not None:
                    return {**stale[0], "stale": True}
                raise HTTPException(429, RATE_LIMIT_MESSAGE)
            try:
                result = analyze(ticker, fetchers=fetchers, cache_dir=cache_dir)
            except UnknownTicker:
                raise HTTPException(404, "Unknown ticker.") from None
            except Exception:
                log.exception("analysis failed for %s", ticker)
                stale = cache.get(key, None)
                if stale is None:
                    raise HTTPException(503, UNAVAILABLE_MESSAGE) from None
                return {**stale[0], "stale": True}
            try:
                cache.set(key, result)
            except Exception:
                log.exception("cache write failed for %s", ticker)
            return {**result, "stale": False}

    @app.get("/api/analysis/{ticker}/narrative")
    def api_narrative(ticker: str, request: Request) -> dict:
        ticker = normalize(ticker)
        cached = cache.get(f"analysis:{ticker}", None)
        if cached is None:
            raise HTTPException(404, "No analysis for this ticker yet.")
        analysis = cached[0]
        try:
            is_new = worker.get(analysis) is None and not worker.is_inflight(analysis)
        except Exception:
            log.exception("narrative lookup failed for %s", ticker)
            return {"status": "unavailable"}
        # Only a brand-new job costs a Claude call; polling a pending one is free.
        if is_new and not narrative_limiter.try_acquire(client_ip(request)):
            raise HTTPException(429, RATE_LIMIT_MESSAGE)
        try:
            status = worker.request(analysis)
            if status == "ready":
                ready = worker.get(analysis) or {}
                return {"status": "ready", "text": ready.get("text", "")}
        except Exception:
            log.exception("narrative request failed for %s", ticker)
            return {"status": "unavailable"}
        return {"status": status}

    # HEAD as well: uptime monitors and link previews probe the page that way.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> FileResponse:
        page = Path(static_dir) / "index.html"
        if not page.is_file():
            raise HTTPException(404, "Page not built yet.")
        return FileResponse(page)

    app.mount("/static", StaticFiles(directory=static_dir, check_dir=False), name="static")
    return app
