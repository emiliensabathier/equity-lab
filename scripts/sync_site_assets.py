"""Copy the site's shared chrome (tokens, nav, theme script) into static/site/.

Run from the repo root: python scripts/sync_site_assets.py [site_dir]   (default ~/projets/site/app)
Never edit static/site/ by hand: change the site, then rerun this.
"""
from pathlib import Path
import hashlib
import sys

site = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "projets" / "site" / "app"
out = Path(__file__).resolve().parent.parent / "static" / "site"
out.mkdir(parents=True, exist_ok=True)
for name in ("tokens.css", "nav.css", "theme.js"):
    body = (site / "src" / "site" / name).read_text("utf-8").replace("/site/fonts/", "/static/fonts/")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = f"/* GENERATED from emiliensabathier/site src/site/{name}. Do not edit. body-sha256: {digest} */"
    (out / name).write_text(f"{header}\n{body}", "utf-8")
    print(out / name)
