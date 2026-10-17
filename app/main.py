"""Public landing routes only. No model, trading or owner-control endpoints."""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
BUILD_SHA = os.environ.get("BUILD_SHA", "development")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(
        {
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'self'; img-src 'self'; "
                "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
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
    return FileResponse(ROOT / "web" / "index.html", media_type="text/html")


@app.get("/healthz", include_in_schema=False)
async def health():
    return JSONResponse(
        {"status": "ok", "application": "pipmind", "mode": "landing", "build": BUILD_SHA},
        headers={"Cache-Control": "no-store"},
    )


app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
