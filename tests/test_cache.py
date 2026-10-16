import sqlite3
from datetime import UTC, datetime, timedelta

from elab.cache import Cache


def test_cache_respects_max_age(tmp_path):
    cache = Cache(tmp_path / "sub" / "c.db")
    cache.set("k", {"a": 1})
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    with sqlite3.connect(tmp_path / "sub" / "c.db") as conn:
        conn.execute("UPDATE entries SET stored_at = ? WHERE key = ?", (old, "k"))
    assert cache.get("k", timedelta(hours=24)) is None
    value, stored_at = cache.get("k", None)
    assert value == {"a": 1}
    assert stored_at.tzinfo is not None
    assert cache.get("k", timedelta(hours=48)) is not None


def test_cache_missing_key_and_overwrite(tmp_path):
    cache = Cache(tmp_path / "c.db")
    assert cache.get("nope", None) is None
    cache.set("k", {"v": 1})
    cache.set("k", {"v": 2})
    assert cache.get("k", None)[0] == {"v": 2}
