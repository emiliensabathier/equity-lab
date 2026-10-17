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


def test_chartjs_script_is_pinned_with_integrity_hash():
    html = (STATIC / "index.html").read_text("utf-8")
    assert "Chart.js/4.4.1/chart.umd.min.js" in html
    assert 'integrity="sha512-' in html and 'crossorigin="anonymous"' in html
    assert 'id="search-status"' in html and 'id="status"' in html


def test_page_shows_a_bound_when_the_price_lies_beyond_the_growth_bracket():
    js = (STATIC / "app.js").read_text("utf-8")
    assert "implied_average_growth_bound" in js and "implied_growth_side" in js
    assert "normalized_average_growth" in js


def test_page_labels_the_weekly_close_by_its_week():
    assert "week of ${day(a.price_date)}" in (STATIC / "app.js").read_text("utf-8")


def test_page_says_to_reload_when_the_narrative_poll_gives_up():
    js = (STATIC / "app.js").read_text("utf-8")
    assert "The written analysis is still being produced; reload in a few minutes." in js
