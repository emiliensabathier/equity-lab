from pathlib import Path

from fastapi.testclient import TestClient

from elab.api import create_app
from elab.cache import Cache
from elab.engine import Fetchers
from elab.narrative import _FORBIDDEN

STATIC = Path(__file__).resolve().parent.parent / "static"


def _client(tmp_path):
    app = create_app(
        fetchers=Fetchers(statements=None, prices=None, info=None),
        cache=Cache(tmp_path / "c.db"),
        worker=None,
        searcher=lambda q: [],
        cache_dir=tmp_path,
        static_dir=STATIC,
    )
    return TestClient(app)


def test_index_served_and_free_of_forbidden_words(tmp_path):
    client = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="search"' in page.text
    assert "/static/app.js" in page.text and "/static/style.css" in page.text
    for name in ("index.html", "app.js", "style.css"):
        assert client.get(f"/static/{name}").status_code == 200
        hits = [m.group(0) for m in _FORBIDDEN.finditer((STATIC / name).read_text("utf-8"))]
        assert hits == [], f"{name}: {hits}"


def test_page_never_assigns_data_through_innerhtml():
    assert "innerHTML" not in (STATIC / "app.js").read_text("utf-8")


def test_chartjs_is_self_hosted_and_pinned_with_integrity_hash():
    import base64
    import hashlib
    import re

    html = (STATIC / "index.html").read_text("utf-8")
    assert "cdnjs" not in html
    script = r'src="/static/vendor/chart.umd.min.js\?v=4.4.1"\s+integrity="sha512-([^"]+)"'
    pinned = re.search(script, html).group(1)
    digest = hashlib.sha512((STATIC / "vendor" / "chart.umd.min.js").read_bytes()).digest()
    assert base64.b64encode(digest).decode() == pinned


def test_scripts_are_deferred_and_every_static_url_is_versioned():
    import re

    html = (STATIC / "index.html").read_text("utf-8")
    assert len(re.findall(r"<script defer src=", html)) == html.count("<script")
    for url in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        if "/fonts/" not in url:  # fonts are preloaded and fetched from the CSS by plain URL
            assert "?v=" in url, url
    assert 'id="search-status"' in html and 'id="status"' in html


def test_page_shows_a_bound_when_the_price_lies_beyond_the_growth_bracket():
    js = (STATIC / "app.js").read_text("utf-8")
    assert "implied_average_growth_bound" in js and "implied_growth_side" in js
    assert "normalized_average_growth" in js


def test_page_labels_the_weekly_close_by_its_week():
    assert "week of ${day(a.price_date)}" in (STATIC / "app.js").read_text("utf-8")


def test_page_says_to_reload_when_the_narrative_poll_gives_up():
    js = (STATIC / "app.js").read_text("utf-8")
    assert "The written analysis is still being produced. Reload in a few minutes." in js
