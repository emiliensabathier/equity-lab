import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
NGINX = (DEPLOY / "nginx-equity-lab.conf").read_text("utf-8")


def test_nginx_rate_limits_the_api_ahead_of_the_app():
    assert re.search(r"^limit_req_zone \$binary_remote_addr zone=elab_api:\S+ rate=\S+;", NGINX,
                     re.M)
    api = NGINX[NGINX.index("location /api/"):]
    assert re.search(r"limit_req zone=elab_api burst=\d+ nodelay;", api)
    assert "limit_req_status 429;" in NGINX


def test_nginx_sends_security_headers_and_a_csp_matching_the_page():
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', NGINX).group(1)
    assert "script-src 'self' https://cdnjs.cloudflare.com" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp  # the page has no inline script or style
    assert 'add_header X-Content-Type-Options "nosniff" always;' in NGINX
    assert 'add_header Referrer-Policy "strict-origin-when-cross-origin" always;' in NGINX


def test_nginx_waits_for_a_cold_analysis():
    assert NGINX.count("proxy_read_timeout 120s;") == 2
    assert "proxy_read_timeout 60s" not in NGINX


@pytest.mark.parametrize("unit", ["equity-lab.service", "equity-lab-jobs.service"])
def test_units_are_sandboxed_to_their_own_directory(unit):
    text = (DEPLOY / unit).read_text("utf-8")
    for line in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict",
                 "ReadWritePaths=/opt/equity-lab", "ProtectHome=yes"):
        assert f"\n{line}\n" in text, line
