import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
CADDY = (DEPLOY / "Caddyfile-equity-lab").read_text("utf-8")


def test_caddy_sends_security_headers_and_a_csp_matching_the_page():
    csp = re.search(r'Content-Security-Policy "([^"]+)"', CADDY).group(1)
    assert "script-src 'self';" in csp  # Chart.js is served from /static/vendor
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp  # the page has no inline script or style
    assert 'X-Content-Type-Options "nosniff"' in CADDY
    assert 'Referrer-Policy "strict-origin-when-cross-origin"' in CADDY


def test_caddy_caches_versioned_static_files_and_compresses():
    assert "@static path /static/*" in CADDY
    assert 'header @static Cache-Control "public, max-age=31536000, immutable"' in CADDY
    assert "encode zstd gzip" in CADDY


def test_caddy_proxies_to_the_app_and_waits_for_a_cold_analysis():
    assert "reverse_proxy 127.0.0.1:8040" in CADDY
    assert "read_timeout 120s" in CADDY
    assert "trusted_proxies" not in CADDY  # a client-supplied X-Forwarded-For must not survive


@pytest.mark.parametrize("unit", ["equity-lab.service", "equity-lab-jobs.service"])
def test_units_are_sandboxed_to_their_own_directory(unit):
    text = (DEPLOY / unit).read_text("utf-8")
    for line in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict",
                 "ReadWritePaths=/opt/equity-lab", "ProtectHome=yes"):
        assert f"\n{line}\n" in text, line
