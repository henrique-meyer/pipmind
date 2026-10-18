"""Exercise the real HTTP server locally, in CI, and after deployment."""

import argparse
import json
import os
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
        self.links = []
        self.ids = set()
        self.scripts = []
        self.meta = {}
        self.canonicals = []
        self.jsonld = []
        self.in_jsonld = False
        self.h1_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag == "h1":
            self.h1_count += 1
        if tag == "meta":
            self.meta[attrs.get("name", attrs.get("property", ""))] = attrs.get("content", "")
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs.get("href"))
        if tag == "script":
            self.in_jsonld = attrs.get("type") == "application/ld+json"
            if self.in_jsonld:
                self.jsonld.append("")
            else:
                self.scripts.append(attrs)
        for attr in ("src", "href"):
            value = attrs.get(attr, "")
            if value.startswith(("/assets/", "/static/")):
                self.assets.add(value)

    def handle_data(self, data):
        if self.in_jsonld:
            self.jsonld[-1] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_jsonld = False


def fetch(base, path, method="GET"):
    request = Request(urljoin(base.rstrip("/") + "/", path.lstrip("/")), method=method)
    with urlopen(request, timeout=20) as response:
        return response.status, response.headers, response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--build")
    parser.add_argument("--analytics-id", default=os.environ.get("GOOGLE_ANALYTICS_ID", ""))
    parser.add_argument("--site-url", default=os.environ.get("PUBLIC_SITE_URL", ""))
    args = parser.parse_args()
    for attempt in range(30):
        try:
            status, headers, body = fetch(args.url, "/healthz")
            break
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 29:
                raise
            time.sleep(2)
    health = json.loads(body)
    assert status == 200 and health["status"] == "ok"
    assert health["application"] == "pipmind" and health["mode"] == "landing"
    assert headers["Cache-Control"] == "no-store"
    if args.build:
        assert health["build"] == args.build, "Unexpected deployed build"
    status, headers, body = fetch(args.url, "/")
    assert status == 200 and "text/html" in headers["Content-Type"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    html = body.decode()
    assert "In development. No trading activity or track record yet." in html
    assert "Not investment advice" in html
    page = Page()
    page.feed(html)
    assert page.assets and page.links
    assert page.h1_count == 1 and page.meta["description"]
    assert "<!-- seo -->" not in html
    _, robots_headers, robots = fetch(args.url, "/robots.txt")
    assert "text/plain" in robots_headers["Content-Type"]
    if args.site_url:
        origin = args.site_url.rstrip("/").lower()
        assert page.canonicals == [origin + "/"], "Wrong canonical URL"
        assert page.meta["robots"] == "index,follow"
        assert page.meta["og:url"] == origin + "/"
        assert page.meta["og:image"] == page.meta["twitter:image"] == origin + "/assets/logo.png"
        data = json.loads(page.jsonld[0])
        assert data["@type"] == "WebSite" and data["url"] == origin + "/"
        assert data["name"] == "PipMind"
        status, image_headers, image = fetch(args.url, "/assets/logo.png")
        assert status == 200 and image_headers["Content-Type"] == "image/png"
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        assert f"Sitemap: {origin}/sitemap.xml" in robots.decode()
        _, sitemap_headers, sitemap = fetch(args.url, "/sitemap.xml")
        assert "application/xml" in sitemap_headers["Content-Type"]
        root = ET.fromstring(sitemap)
        assert [element.text for element in root.findall(".//{*}loc")] == [origin + "/"]
    else:
        assert not page.canonicals and not page.jsonld
        assert page.meta["robots"] == "noindex,nofollow"
        assert headers["X-Robots-Tag"] == "noindex"
        assert robots == b"User-agent: *\nDisallow: /\n"
        try:
            fetch(args.url, "/sitemap.xml")
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("Unconfigured preview must not publish a sitemap")
    if args.analytics_id:
        assert page.scripts == [
            {"defer": None, "src": "/static/analytics.js", "data-measurement-id": args.analytics_id}
        ], "Unexpected analytics configuration"
        assert "https://www.googletagmanager.com" in headers["Content-Security-Policy"]
    else:
        assert not page.scripts, "Analytics must be disabled when unset"
        assert "google" not in headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in headers["Content-Security-Policy"]
    for asset in page.assets:
        status, _, content = fetch(args.url, asset)
        assert status == 200 and len(content) > 100, f"Broken asset: {asset}"
    for link in page.links:
        if link.startswith("#"):
            assert link[1:] in page.ids, f"Broken anchor: {link}"
        elif link.startswith("https:"):
            assert urlsplit(link).hostname in {
                "github.com",
                "www.hmeyer.com",
            }, "Unexpected outbound destination"
    assert fetch(args.url, "/", "HEAD")[2] == b""
    for private_path in (
        "/.env",
        "/AGENTS.md",
        "/.git/config",
        "/infra/main.bicep",
        "/requirements.md",
        "/assets/README.md",
        "/docs",
        "/openapi.json",
        "/orders",
        "/static/../app/main.py",
        "/assets/../app/main.py",
    ):
        try:
            fetch(args.url, private_path)
        except HTTPError as error:
            assert error.code == 404, f"Unexpected private route status: {error.code}"
        else:
            raise AssertionError(f"Private or nonexistent route exposed: {private_path}")
    print("PASS: build, health, page, SEO, analytics, assets, anchors and route isolation")


if __name__ == "__main__":
    main()
