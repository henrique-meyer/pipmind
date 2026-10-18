"""Public landing routes only. No model, trading or owner-control endpoints."""

import os
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
BUILD_SHA = os.environ.get("BUILD_SHA", "development")
GOOGLE_ANALYTICS_ID = os.environ.get("GOOGLE_ANALYTICS_ID", "")
if GOOGLE_ANALYTICS_ID and not re.fullmatch(r"G-[A-Z0-9]{4,20}", GOOGLE_ANALYTICS_ID):
    raise ValueError(
        "GOOGLE_ANALYTICS_ID must be a GA4 measurement ID (G- followed by letters/digits)"
    )

BASE_CSP = (
    "default-src 'none'; style-src 'self'; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
)
ANALYTICS_CSP = (
    "script-src 'self' https://www.googletagmanager.com; "
    "img-src 'self' https://www.googletagmanager.com https://*.google-analytics.com; "
    "connect-src https://www.googletagmanager.com https://*.google-analytics.com https://*.google.com"
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "Content-Security-Policy": BASE_CSP
            + (
                ANALYTICS_CSP
                if GOOGLE_ANALYTICS_ID and request.url.path == "/"
                else "img-src 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
    )
    response.headers.setdefault("Cache-Control", "public, max-age=300")
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
    return HTMLResponse(html.replace("<!-- analytics -->", analytics))


@app.get("/healthz", include_in_schema=False)
async def health():
    return JSONResponse(
        {"status": "ok", "application": "pipmind", "mode": "landing", "build": BUILD_SHA},
        headers={"Cache-Control": "no-store"},
    )


app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
