"""Landing analytics configuration must not relax private route isolation."""

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import main
from app.seo import public_site_url
from scripts.smoke import Page, share_image_corners


@pytest.mark.parametrize("measurement_id", ["", "G-TEST123456"])
@pytest.mark.parametrize("origin", ["", "https://site.example"])
def test_analytics_is_optional_and_landing_only(monkeypatch, measurement_id, origin):
    monkeypatch.setattr(main, "GOOGLE_ANALYTICS_ID", measurement_id)
    monkeypatch.setattr(main, "PUBLIC_SITE_URL", origin)
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
        for path in ("/healthz", "/orders", "/.env", "/robots.txt"):
            private = client.get(path)
            assert "google" not in private.headers["Content-Security-Policy"]
            assert "analytics.js" not in private.text
            assert 'property="og:' not in private.text
            assert 'name="twitter:' not in private.text
            assert private.headers["X-Robots-Tag"] == "noindex"
        assert client.get("/orders").status_code == 404


def test_seo_uses_configured_origin_not_request_headers(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_SITE_URL", "https://site.example")
    with TestClient(main.app) as client:
        response = client.get(
            "/?ignored=secret",
            headers={"Host": "attacker.example", "X-Forwarded-Host": "attacker.example"},
        )
        page = Page()
        page.feed(response.text)
        assert page.canonicals == ["https://site.example/"]
        assert page.meta["og:url"] == page.canonicals[0]
        assert page.meta["og:image"] == "https://site.example/assets/logo.png"
        head = Page()
        head.feed(response.text.split("</head>", 1)[0])
        assert head.meta["og:title"] == head.meta["twitter:title"] == head.title
        assert (
            head.meta["og:description"]
            == head.meta["twitter:description"]
            == head.meta["description"]
        )
        assert head.meta["og:type"] == "website"
        assert head.meta["twitter:card"] == "summary"
        assert head.meta["og:image"] == head.meta["twitter:image"] == page.meta["og:image"]
        assert head.meta["og:image:type"] == "image/png"
        assert head.meta["og:image:alt"] == "PipMind"
        image = client.get(head.meta["og:image"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", image.content[16:24])
        assert width >= 300 and height >= 300
        assert (int(head.meta["og:image:width"]), int(head.meta["og:image:height"])) == (
            width,
            height,
        )
        assert page.meta["robots"] == "index,follow"
        assert "X-Robots-Tag" not in response.headers
        assert "attacker.example" not in response.text and "ignored=secret" not in response.text
        data = json.loads(page.jsonld[0])
        assert data["url"] == page.canonicals[0] and data["@type"] == "WebSite"
        digest = base64.b64encode(hashlib.sha256(page.jsonld[0].encode()).digest()).decode()
        assert f"'sha256-{digest}'" in response.headers["Content-Security-Policy"]
        sitemap = client.get("/sitemap.xml")
        assert "https://site.example/</loc>" in sitemap.text
        assert "attacker.example" not in sitemap.text
        assert sitemap.headers["X-Robots-Tag"] == "noindex"
        assert client.head("/sitemap.xml").content == b""
        assert client.head("/robots.txt").content == b""


def test_share_image_corners_are_opaque_navy_and_favicon_is_an_icon():
    with TestClient(main.app) as client:
        image = client.get("/assets/logo.png")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        corners, dimensions = share_image_corners(image.content)
        assert dimensions == (1024, 1024)
        assert corners == ((12, 35, 64, 255),) * 4
        icon = client.get("/favicon.ico")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/")
        assert icon.content.startswith(b"\x00\x00\x01\x00")
        assert not icon.content.startswith(b"{")
        assert client.head("/favicon.ico").status_code == 200
        assert client.head("/favicon.ico").content == b""


def test_unconfigured_preview_does_not_advertise_indexable_urls(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_SITE_URL", "")
    with TestClient(main.app) as client:
        response = client.get("/")
        page = Page()
        page.feed(response.text)
        assert page.meta["robots"] == "noindex,nofollow"
        assert not page.canonicals and not page.jsonld
        assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
        assert client.get("/sitemap.xml").status_code == 404
        assert response.headers["X-Robots-Tag"] == "noindex"


@pytest.mark.parametrize("value", ["", "https://site.example/", "https://SITE.EXAMPLE"])
def test_public_origin_normalization(value):
    assert public_site_url(value) == ("https://site.example" if value else "")


@pytest.mark.parametrize(
    "value",
    [
        "http://site.example",
        "https://site.example/path",
        "https://site.example?private=value",
        "https://site.example/#fragment",
        "https://user:password@site.example",
        "https://localhost",
        'https://site.example"><script>',
        "https://site.example\n",
        "https://127.0.0.1",
        "https://site.example:443",
        "https://-bad.example",
        "https://site.example\\evil",
    ],
)
def test_unsafe_or_noncanonical_origins_rejected(value):
    with pytest.raises(ValueError, match="PUBLIC_SITE_URL must be"):
        public_site_url(value)


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
