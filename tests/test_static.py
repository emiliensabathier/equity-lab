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
