"""Landing analytics configuration must not relax private route isolation."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.mark.parametrize("measurement_id", ["", "G-TEST123456"])
def test_analytics_is_optional_and_landing_only(monkeypatch, measurement_id):
    monkeypatch.setattr(main, "GOOGLE_ANALYTICS_ID", measurement_id)
    with TestClient(main.app) as client:
        response = client.get("/")
        assert response.status_code == 200
        csp = response.headers["Content-Security-Policy"]
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert ("/static/analytics.js" in response.text) == bool(measurement_id)
        assert ("google-analytics.com" in csp) == bool(measurement_id)
        assert "<!-- analytics -->" not in response.text
        assert client.head("/").content == b""
        if measurement_id:
            assert f'data-measurement-id="{measurement_id}"' in response.text
            script = client.get("/static/analytics.js")
            assert script.status_code == 200
            assert "javascript" in script.headers["content-type"]
        for path in ("/healthz", "/orders", "/.env"):
            private = client.get(path)
            assert "google" not in private.headers["Content-Security-Policy"]
            assert "analytics.js" not in private.text
        assert client.get("/orders").status_code == 404


@pytest.mark.parametrize("value", ['G-ABCD"<script>', "GTM-ABC123", " G-ABC123", "g-abc123"])
def test_invalid_measurement_id_prevents_startup(value):
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env={**os.environ, "GOOGLE_ANALYTICS_ID": value},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "GOOGLE_ANALYTICS_ID must be a GA4 measurement ID" in result.stderr
