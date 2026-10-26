"""Public landing and separately authenticated read-only owner observations."""

import os
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.dashboard import PRIVATE_CSP, install_dashboard
from app.seo import metadata, public_site_url, schema_csp_source

ROOT = Path(__file__).resolve().parent.parent
BUILD_SHA = os.environ.get("BUILD_SHA", "development")
PUBLIC_SITE_URL = public_site_url(os.environ.get("PUBLIC_SITE_URL", ""))
GOOGLE_ANALYTICS_ID = os.environ.get("GOOGLE_ANALYTICS_ID", "")
if GOOGLE_ANALYTICS_ID and not re.fullmatch(r"G-[A-Z0-9]{4,20}", GOOGLE_ANALYTICS_ID):
    raise ValueError(
        "GOOGLE_ANALYTICS_ID must be a GA4 measurement ID (G- followed by letters/digits)"
    )

BASE_CSP = (
    "default-src 'none'; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
)


def page_csp():
    directives = [BASE_CSP]
    scripts = [schema_csp_source(PUBLIC_SITE_URL)] if PUBLIC_SITE_URL else []
    images = ["'self'"]
    if GOOGLE_ANALYTICS_ID:
        scripts.extend(["'self'", "https://www.googletagmanager.com"])
        images.extend(["https://www.googletagmanager.com", "https://*.google-analytics.com"])
        directives.append(
            "connect-src https://www.googletagmanager.com "
            "https://*.google-analytics.com https://*.google.com; "
        )
    if scripts:
        directives.append("script-src " + " ".join(scripts) + "; ")
    directives.append("img-src " + " ".join(images))
    return "".join(directives)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    private = request.url.path == "/app" or request.url.path.startswith("/app/")
    response.headers.update(
        {
            "Content-Security-Policy": (
                PRIVATE_CSP
                if private
                else (
                    page_csp()
                    if request.url.path == "/" and response.status_code == 200
                    else BASE_CSP + "img-src 'self'"
                )
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
    )
    public_path = request.url.path == "/" or request.url.path.startswith(("/assets/", "/static/"))
    if not PUBLIC_SITE_URL or not public_path or response.status_code != 200:
        response.headers["X-Robots-Tag"] = "noindex"
    response.headers.setdefault("Cache-Control", "public, max-age=300")
    if private:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Vary"] = "Cookie"
    return response


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def landing():
    analytics = (
        '<script defer src="/static/analytics.js" '
        f'data-measurement-id="{GOOGLE_ANALYTICS_ID}"></script>'
        if GOOGLE_ANALYTICS_ID
        else ""
    )
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("<!-- seo -->", metadata(PUBLIC_SITE_URL)).replace(
            "<!-- analytics -->", analytics
        )
    )


@app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def robots():
    rules = (
        f"User-agent: *\nAllow: /\nSitemap: {PUBLIC_SITE_URL}/sitemap.xml\n"
        if PUBLIC_SITE_URL
        else "User-agent: *\nDisallow: /\n"
    )
    return PlainTextResponse(rules)


@app.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
async def sitemap():
    if not PUBLIC_SITE_URL:
        return PlainTextResponse("Not found", status_code=404)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{PUBLIC_SITE_URL}/</loc></url></urlset>\n",
        media_type="application/xml",
    )


@app.get("/healthz", include_in_schema=False)
async def health():
    return JSONResponse(
        {"status": "ok", "application": "pipmind", "mode": "landing", "build": BUILD_SHA},
        headers={"Cache-Control": "no-store"},
    )


install_dashboard(app, ROOT)

app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
