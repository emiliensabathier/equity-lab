import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from elab import api as api_mod
from elab.api import RATE_LIMIT_MESSAGE, SlidingWindowLimiter, create_app
from elab.cache import Cache
from elab.engine import Fetchers
from elab.resolve import UnknownTicker

ANALYSIS = {"ticker": "ACME", "as_of": "2026-10-17", "value": 12.5}
GOOD = "Upside is 12.5 over 5 years."


class StubWorker:
    """Records requests; `states` is consumed one per request call."""

    def __init__(self):
        self.result = None
        self.inflight = False
        self.requests = 0

    def get(self, analysis):
        return self.result

    def is_inflight(self, analysis):
        return self.inflight

    def request(self, analysis):
        self.requests += 1
        if self.result is not None:
            return self.result["status"]
        self.inflight = True
        return "pending"


@pytest.fixture
def env(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>equity-lab</h1>")
    (static / "app.js").write_text("// js")
    state = {"calls": 0, "behavior": lambda t: {**ANALYSIS, "ticker": t}, "now": 0.0, "searches": 0,
             "queued": [], "inline": True}

    def fake_analyze(ticker, *, fetchers, cache_dir):
        state["calls"] += 1
        return state["behavior"](ticker)

    monkeypatch.setattr(api_mod, "analyze", fake_analyze)
    cache = Cache(tmp_path / "c.db")
    worker = StubWorker()
    fetchers = Fetchers(statements=None, prices=None, info=None)
    quotes = [{"symbol": "ACME", "longname": "Acme Inc", "exchange": "NMS", "quoteType": "EQUITY"}]

    def searcher(q):
        state["searches"] += 1
        return quotes

    app = create_app(
        fetchers=fetchers,
        cache=cache,
        worker=worker,
        searcher=searcher,
        cache_dir=tmp_path,
        static_dir=static,
        clock=lambda: state["now"],
        background=lambda job: job() if state["inline"] else state["queued"].append(job),
    )
    return {"client": TestClient(app), "cache": cache, "worker": worker, "state": state}


def test_analysis_served_from_cache_without_fetch(env):
    env["cache"].set("analysis:ACME", ANALYSIS)
    body = env["client"].get("/api/analysis/acme").json()
    assert body == {**ANALYSIS, "stale": False, "has_group": False}
    assert env["state"]["calls"] == 0


def test_cold_analysis_runs_once_then_cached(env):
    first = env["client"].get("/api/analysis/ACME")
    second = env["client"].get("/api/analysis/ACME")
    assert first.status_code == second.status_code == 200
    assert first.json()["stale"] is False
    assert env["state"]["calls"] == 1


def test_concurrent_cold_requests_run_one_analysis(env):
    def slow(ticker):
        time.sleep(0.2)
        return {**ANALYSIS, "ticker": ticker}

    env["state"]["behavior"] = slow
    codes = []

    def hit():
        codes.append(env["client"].get("/api/analysis/ACME").status_code)

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200, 200]
    assert env["state"]["calls"] == 1


def test_expired_copy_is_served_at_once_while_a_refresh_replaces_it(env, monkeypatch):
    env["cache"].set("analysis:ACME", ANALYSIS)
    monkeypatch.setattr(api_mod.config, "ANALYSIS_TTL", timedelta(0))
    env["state"]["inline"] = False
    env["state"]["behavior"] = lambda t: {**ANALYSIS, "value": 99.0}

    res = env["client"].get("/api/analysis/ACME")

    assert res.json() == {**ANALYSIS, "stale": False, "refreshing": True,
                          "has_group": False}
    assert env["state"]["calls"] == 0  # nothing computed while the visitor waited
    [job] = env["state"]["queued"]
    job()
    assert env["cache"].get("analysis:ACME", None)[0]["value"] == 99.0


def test_requests_during_a_refresh_queue_it_once(env, monkeypatch):
    env["cache"].set("analysis:ACME", ANALYSIS)
    monkeypatch.setattr(api_mod.config, "ANALYSIS_TTL", timedelta(0))
    env["state"]["inline"] = False
    for _ in range(3):
        assert env["client"].get("/api/analysis/ACME").json()["refreshing"] is True
    assert len(env["state"]["queued"]) == 1


def test_a_failed_refresh_keeps_the_old_copy_and_can_be_retried(env, monkeypatch):
    env["cache"].set("analysis:ACME", ANALYSIS)
    monkeypatch.setattr(api_mod.config, "ANALYSIS_TTL", timedelta(0))
    env["state"]["behavior"] = lambda t: (_ for _ in ()).throw(RuntimeError("secret boom"))
    res = env["client"].get("/api/analysis/ACME")
    assert res.status_code == 200
    assert res.json() == {**ANALYSIS, "stale": False, "refreshing": True,
                          "has_group": False}
    assert "secret boom" not in res.text
    assert env["cache"].get("analysis:ACME", None)[0] == ANALYSIS
    env["client"].get("/api/analysis/ACME")
    assert env["state"]["calls"] == 2  # the failed ticker is not stuck as "refreshing"


def test_upstream_failure_without_cache_is_503(env):
    env["state"]["behavior"] = lambda t: (_ for _ in ()).throw(RuntimeError("secret boom"))
    res = env["client"].get("/api/analysis/ACME")
    assert res.status_code == 503
    assert res.json() == {"detail": "Data source unavailable, try again in a minute."}
    assert "secret boom" not in res.text


def test_unknown_ticker_is_404(env):
    env["state"]["behavior"] = lambda t: (_ for _ in ()).throw(UnknownTicker("nope"))
    res = env["client"].get("/api/analysis/ZZZZ")
    assert res.status_code == 404
    assert res.json() == {"detail": "Unknown ticker."}


@pytest.mark.parametrize("bad", ["bad%20ticker", "-X", "A" * 16, "A$B"])
def test_invalid_ticker_is_422(env, bad):
    assert env["client"].get(f"/api/analysis/{bad}").status_code == 422
    assert env["client"].get(f"/api/analysis/{bad}/narrative").status_code == 422


def test_cold_rate_limit_returns_429_but_cached_still_served(env):
    for i in range(10):
        assert env["client"].get(f"/api/analysis/T{i}").status_code == 200
    blocked = env["client"].get("/api/analysis/T10")
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": RATE_LIMIT_MESSAGE}
    assert env["client"].get("/api/analysis/T0").status_code == 200  # cached, not counted
    env["state"]["now"] = 3601.0  # window slides
    assert env["client"].get("/api/analysis/T10").status_code == 200


def test_forwarded_for_only_trusted_from_loopback(env):
    # TestClient's peer is "testclient": the header must be ignored, so one budget is shared.
    for i in range(10):
        env["client"].get(f"/api/analysis/T{i}", headers={"X-Forwarded-For": f"9.9.9.{i}"})
    res = env["client"].get("/api/analysis/T10", headers={"X-Forwarded-For": "1.1.1.1"})
    assert res.status_code == 429


def test_forwarded_for_first_hop_from_loopback(tmp_path, monkeypatch):
    class Req:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}

    assert api_mod.client_ip(Req()) == "203.0.113.7"
    Req.client = type("C", (), {"host": "198.51.100.2"})()
    assert api_mod.client_ip(Req()) == "198.51.100.2"
    Req.client = None
    Req.headers = {}
    assert api_mod.client_ip(Req()) == "unknown"


def test_narrative_404_without_analysis(env):
    assert env["client"].get("/api/analysis/ACME/narrative").status_code == 404


def test_narrative_pending_then_ready(env):
    env["cache"].set("analysis:ACME", ANALYSIS)
    url = "/api/analysis/ACME/narrative"
    assert env["client"].get(url).json() == {"status": "pending"}
    env["worker"].result = {"status": "ready", "text": GOOD}
    env["worker"].inflight = False
    assert env["client"].get(url).json() == {"status": "ready", "text": GOOD}


def test_narrative_unavailable(env):
    env["cache"].set("analysis:ACME", ANALYSIS)
    env["worker"].result = {"status": "unavailable"}
    assert env["client"].get("/api/analysis/ACME/narrative").json() == {"status": "unavailable"}


def test_narrative_polling_pending_job_is_not_counted_but_new_jobs_are(env):
    for i in range(6):
        env["cache"].set(f"analysis:T{i}", {**ANALYSIS, "ticker": f"T{i}", "value": i})
    first = env["client"].get("/api/analysis/T0/narrative")
    assert first.json() == {"status": "pending"}
    env["worker"].inflight = True  # same job still running: polls are free
    for _ in range(20):
        assert env["client"].get("/api/analysis/T0/narrative").status_code == 200
    env["worker"].inflight = False
    codes = []
    for i in range(1, 6):
        codes.append(env["client"].get(f"/api/analysis/T{i}/narrative").status_code)
        env["worker"].inflight = False
    assert codes == [200, 200, 200, 200, 429]


def test_search_returns_matches(env):
    res = env["client"].get("/api/search", params={"q": "acme"})
    assert res.json() == [{"ticker": "ACME", "name": "Acme Inc", "exchange": "NMS", "brand": "",
                          "branch": ""}]


def test_search_names_the_company_that_owns_a_brand(env):
    res = env["client"].get("/api/search", params={"q": "Zara"})
    # The shipped Inditex breakdown puts Zara in its first segment.
    assert res.json()[0] == {"ticker": "ITX.MC", "name": "Inditex", "exchange": "", "brand": "Zara",
                             "branch": "zara-zara-home-lefties"}


GROUP = {
    "ticker": "ITX.MC", "name": "Inditex", "currency": "EUR",
    "years": ["FY2024", "FY2025"], "year_ends": ["2025-01-31", "2026-01-31"],
    "profit_label": "EBIT", "sources": [{"title": "AR", "url": "https://example.com"}],
    "total": {"revenue": [10, 12], "operating_profit": [2, 3]},
    "branches": [
        {"name": "Zara", "revenue": [7, 8], "operating_profit": [1.5, 2], "brands": ["Zara Home"]},
        {"name": "Bershka", "revenue": [3, 4], "operating_profit": [0.5, 1], "brands": []},
    ],
}


@pytest.fixture
def group_client(tmp_path):
    from elab.groups import validate

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>equity-lab</h1>")
    app = create_app(
        fetchers=Fetchers(statements=None, prices=None, info=None), cache=Cache(tmp_path / "c.db"),
        worker=StubWorker(), searcher=lambda q: [], cache_dir=tmp_path, static_dir=static,
        groups={"ITX.MC": validate(GROUP)},
    )
    return TestClient(app)


def test_search_links_a_brand_to_its_branch(group_client):
    assert group_client.get("/api/search", params={"q": "zara"}).json()[0]["branch"] == "zara"
    assert group_client.get("/api/search", params={"q": "zara home"}).json()[0]["branch"] == "zara"


def test_group_endpoint_serves_branches(group_client):
    body = group_client.get("/api/group/itx.mc").json()
    assert [b["id"] for b in body["branches"]] == ["zara", "bershka"]
    assert body["branches"][0]["comparables"] == []


def test_group_endpoint_is_404_for_a_company_without_breakdown(group_client):
    assert group_client.get("/api/group/AAPL").status_code == 404
    assert group_client.get("/api/group/bad$").status_code == 422


def test_search_results_are_cached_for_ten_minutes_by_normalized_query(env):
    client, state = env["client"], env["state"]
    first = client.get("/api/search", params={"q": " Acme "}).json()
    assert client.get("/api/search", params={"q": "acme"}).json() == first
    assert state["searches"] == 1
    state["now"] += 10 * 60
    client.get("/api/search", params={"q": "ACME"})
    assert state["searches"] == 2


def test_search_is_limited_per_address_to_sixty_a_minute(env):
    client, state = env["client"], env["state"]
    for _ in range(60):
        assert client.get("/api/search", params={"q": "acme"}).status_code == 200
    res = client.get("/api/search", params={"q": "acme"})
    assert res.status_code == 429
    assert isinstance(res.json()["detail"], str)
    state["now"] += 60
    assert client.get("/api/search", params={"q": "acme"}).status_code == 200


@pytest.mark.parametrize("query", ["", " a ", "x" * 61])
def test_search_outside_the_query_length_neither_searches_nor_caches(env, query, monkeypatch):
    # A 2 KB query must not become a cache key: out-of-range queries stop before both.
    calls = []
    monkeypatch.setattr(api_mod, "search", lambda *args: calls.append(args) or [])
    monkeypatch.setattr(api_mod.TtlCache, "set", lambda *args: calls.append(args))
    monkeypatch.setattr(api_mod.TtlCache, "get", lambda *args: calls.append(args))
    assert env["client"].get("/api/search", params={"q": query}).json() == []
    assert calls == []


def test_search_short_query_is_empty(env):
    assert env["client"].get("/api/search", params={"q": "a"}).json() == []
    assert env["client"].get("/api/search").json() == []


def test_search_failure_is_503(tmp_path):
    def boom(q):
        raise RuntimeError("yahoo down")

    app = create_app(
        fetchers=Fetchers(statements=None, prices=None, info=None),
        cache=Cache(tmp_path / "c.db"),
        worker=StubWorker(),
        searcher=boom,
        cache_dir=tmp_path,
        static_dir=tmp_path,
    )
    res = TestClient(app).get("/api/search", params={"q": "acme"})
    assert res.status_code == 503
    assert "yahoo down" not in res.text


def test_index_and_static_served(env):
    assert "equity-lab" in env["client"].get("/").text
    assert env["client"].get("/static/app.js").text == "// js"


def test_index_missing_is_404_not_500(tmp_path):
    app = create_app(
        fetchers=Fetchers(statements=None, prices=None, info=None),
        cache=Cache(tmp_path / "c.db"),
        worker=StubWorker(),
        searcher=lambda q: [],
        cache_dir=tmp_path,
        static_dir=tmp_path / "missing",
    )
    assert TestClient(app).get("/").status_code == 404


def test_limiter_slides_and_isolates_keys():
    now = [0.0]
    limiter = SlidingWindowLimiter(2, timedelta(seconds=10), lambda: now[0])
    assert limiter.try_acquire("a") and limiter.try_acquire("a")
    assert not limiter.try_acquire("a")
    assert limiter.try_acquire("b")
    now[0] = 10.0
    assert limiter.try_acquire("a")


def test_cold_limit_exceeded_serves_stale_copy_instead_of_429(env, monkeypatch):
    for i in range(10):
        env["client"].get(f"/api/analysis/T{i}")
    env["cache"].set("analysis:OLD", ANALYSIS)
    monkeypatch.setattr(api_mod.config, "ANALYSIS_TTL", timedelta(0))
    res = env["client"].get("/api/analysis/OLD")
    assert res.status_code == 200
    assert res.json() == {**ANALYSIS, "stale": True, "has_group": False}


def test_trailing_newline_ticker_is_422(env):
    assert env["client"].get("/api/analysis/AAPL%0A").status_code == 422


def test_cache_write_failure_still_returns_result(env, monkeypatch):
    def broken_set(key, value):
        raise OSError("disk full")

    monkeypatch.setattr(env["cache"], "set", broken_set)
    res = env["client"].get("/api/analysis/ACME")
    assert res.status_code == 200
    assert res.json()["stale"] is False


def test_narrative_worker_failure_is_unavailable_shape(env):
    env["cache"].set("analysis:ACME", ANALYSIS)

    def boom(analysis):
        raise RuntimeError("worker broke")

    env["worker"].request = boom
    res = env["client"].get("/api/analysis/ACME/narrative")
    assert res.json() == {"status": "unavailable"}


def test_unexpected_error_is_503_json(env, monkeypatch):
    def boom(key, max_age):
        raise RuntimeError("db gone")

    monkeypatch.setattr(env["cache"], "get", boom)
    client = TestClient(env["client"].app, raise_server_exceptions=False)
    res = client.get("/api/analysis/ACME")
    assert res.status_code == 503
    assert "db gone" not in res.text


def test_ipv4_mapped_loopback_trusts_forwarded_for():
    class Req:
        client = type("C", (), {"host": "::ffff:127.0.0.1"})()
        headers = {"x-forwarded-for": "203.0.113.7"}

    assert api_mod.client_ip(Req()) == "203.0.113.7"


def test_limiter_evicts_idle_keys():
    now = [0.0]
    limiter = SlidingWindowLimiter(2, timedelta(seconds=10), lambda: now[0])
    limiter.try_acquire("a")
    now[0] = 20.0
    limiter.try_acquire("b")
    assert "a" not in limiter._hits


def test_search_cache_drops_the_oldest_entry_past_its_size():
    from elab.api import TtlCache

    cache = TtlCache(timedelta(minutes=10), max_size=2, clock=lambda: 0.0)
    for key in ("a", "b", "c"):
        cache.set(key, [{"ticker": key}])
    assert cache.get("a") is None
    assert cache.get("c") == [{"ticker": "c"}]


def test_index_answers_head_requests(env):
    assert env["client"].head("/").status_code == 200


def test_analysis_says_whether_the_company_has_branches(group_client, monkeypatch):
    monkeypatch.setattr(api_mod, "analyze", lambda t, **kw: {"ticker": t, "as_of": "2026-10-21"})
    assert group_client.get("/api/analysis/ITX.MC").json()["has_group"] is True
    assert group_client.get("/api/analysis/AAPL").json()["has_group"] is False
