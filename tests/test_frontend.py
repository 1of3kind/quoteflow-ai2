"""Frontend serving: landing page, app UI, and static/API coexistence."""

from pathlib import Path

from fastapi.testclient import TestClient  # sync client for static checks


def _client():
    from api.main import app
    return TestClient(app)


def test_landing_page_served_at_root():
    with _client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "QuoteFlow" in r.text
        assert "Instant AI quotes" in r.text or "instant, explainable quote" in r.text
        assert "text/html" in r.headers["content-type"]


def test_landing_has_cta_to_app():
    with _client() as c:
        r = c.get("/")
        assert '/app.html#signup' in r.text
        assert '/app.html#login' in r.text


def test_app_ui_served():
    with _client() as c:
        r = c.get("/app.html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # core app sections present
        for marker in ('auth-login', 'auth-signup', 'tab-dashboard', 'tab-assistant',
                       'tab-quotes', 'tab-jobs', 'tab-schedule', 'tab-materials',
                       'tab-billing', '/auth/login', '/assistant/command'):
            assert marker in r.text, f"app UI missing {marker}"


def test_app_ui_has_token_handling_for_email_links():
    with _client() as c:
        r = c.get("/app.html")
        # deep-link handling for verify-email and reset flows
        assert 'verify-email' in r.text
        assert "params.get('token')" in r.text


def test_api_info_moved_off_root():
    with _client() as c:
        r = c.get("/api")
        assert r.status_code == 200
        assert r.json()["service"] == "QuoteFlow AI"
        assert r.json()["app"] == "/app.html"


def test_static_does_not_shadow_api():
    with _client() as c:
        # /billing/plans must return JSON from the API, not a 404 from static
        r = c.get("/billing/plans")
        assert r.status_code == 200
        assert isinstance(r.json()["plans"], list)
        # health stays JSON
        assert c.get("/health").json()["status"] in ("healthy", "degraded")


def test_unknown_path_gets_static_404_not_api_leak():
    with _client() as c:
        r = c.get("/no-such-page")
        assert r.status_code == 404


def test_app_javascript_is_syntactically_valid():
    """The whole app breaks if a single JS syntax error ships — parse it."""
    import re
    import shutil
    import subprocess
    html = open("frontend/static/app.html", encoding="utf-8").read()
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    script = Path("app_syntax_check.tmp.js")
    script.write_text(js, encoding="utf-8")
    try:
        node = shutil.which("node")
        if not node:
            import pytest
            pytest.skip("node not available for JS syntax check")
        proc = subprocess.run(["node", "--check", str(script)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"app.html JS syntax error:\n{proc.stderr}"
    finally:
        script.unlink(missing_ok=True)
