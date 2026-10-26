"""Exercise the real HTTP server locally, in CI, and after deployment."""

import argparse
import json
import os
import struct
import time
import xml.etree.ElementTree as ET
import zlib
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
        self.in_title = False
        self.title = ""
        self.h1_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
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
        if self.in_title:
            self.title += data
        if self.in_jsonld:
            self.jsonld[-1] += data

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script":
            self.in_jsonld = False


def _paeth(left, up, upper_left):
    estimate = left + up - upper_left
    nearest_left = abs(estimate - left)
    nearest_up = abs(estimate - up)
    if nearest_left <= nearest_up and nearest_left <= abs(estimate - upper_left):
        return left
    if nearest_up <= abs(estimate - upper_left):
        return up
    return upper_left


def share_image_corners(data):
    """Corner RGBA tuples and size for a non-interlaced 8-bit PNG."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("Share image is not a PNG")
    position = 8
    width = height = color = depth = interlace = None
    payload = bytearray()
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        chunk = data[position + 8 : position + 8 + length]
        position += length + 12
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            payload.extend(chunk)
        elif kind == b"IEND":
            break
    if not (width and height and depth == 8 and interlace == 0 and color in (2, 6)):
        raise AssertionError("Share image must be a non-interlaced 8-bit RGB PNG")
    channels = 3 if color == 2 else 4
    raw = zlib.decompress(bytes(payload))
    stride = width * channels
    rows = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                value = row[index]
            elif filter_type == 1:
                value = row[index] + left
            elif filter_type == 2:
                value = row[index] + up
            elif filter_type == 3:
                value = row[index] + (left + up) // 2
            elif filter_type == 4:
                value = row[index] + _paeth(left, up, upper_left)
            else:
                raise AssertionError("Unsupported PNG filter")
            row[index] = value & 255
        previous = row
        rows.append(row)

    def pixel(x, y):
        start = x * channels
        red, green, blue = rows[y][start : start + 3]
        alpha = 255 if channels == 3 else rows[y][start + 3]
        return red, green, blue, alpha

    corners = (
        pixel(0, 0),
        pixel(width - 1, 0),
        pixel(0, height - 1),
        pixel(width - 1, height - 1),
    )
    return corners, (width, height)


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
        head = Page()
        head.feed(html.split("</head>", 1)[0])
        assert head.canonicals == page.canonicals
        assert head.meta["og:title"] == head.meta["twitter:title"] == head.title
        assert head.title
        assert (
            head.meta["og:description"]
            == head.meta["twitter:description"]
            == head.meta["description"]
        )
        assert head.meta["og:url"] == head.canonicals[0]
        assert head.meta["og:type"] == "website"
        assert head.meta["og:image"] == head.meta["twitter:image"] == page.meta["og:image"]
        assert head.meta["og:image:type"] == "image/png"
        assert head.meta["og:image:alt"] == "PipMind"
        assert head.meta["twitter:card"] == "summary"
        data = json.loads(page.jsonld[0])
        assert data["@type"] == "WebSite" and data["url"] == origin + "/"
        assert data["name"] == "PipMind"
        status, image_headers, image = fetch(args.url, "/assets/logo.png")
        assert status == 200 and image_headers["Content-Type"] == "image/png"
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", image[16:24])
        assert width >= 300 and height >= 300, "Share image is too small"
        corners, dimensions = share_image_corners(image)
        assert dimensions == (width, height) == (1024, 1024)
        assert corners == ((12, 35, 64, 255),) * 4, "Share image corners are not opaque navy"
        assert (int(head.meta["og:image:width"]), int(head.meta["og:image:height"])) == (
            width,
            height,
        ), "Share image dimensions differ from metadata"
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
    status, icon_headers, icon = fetch(args.url, "/favicon.ico")
    assert status == 200 and icon_headers["Content-Type"].startswith("image/")
    assert icon.startswith(b"\x00\x00\x01\x00")
    assert fetch(args.url, "/favicon.ico", "HEAD")[0] == 200
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
