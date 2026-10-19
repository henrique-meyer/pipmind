"""Private owner JSON controls. The static shell contains no account/mind data."""

import asyncio
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from pipmind.shadow.common import ShadowError, bounded_json, now
from pipmind.shadow.library import RobotLibrary
from pipmind.shadow.robot import MAX_ARCHIVE

ASSETS = Path(__file__).with_name("owner_web")


async def read_body(request, limit=8192):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "robot_request_size")
    return bytes(raw)


async def fields(request, required, optional=()):
    body = bounded_json(await read_body(request))
    if not isinstance(body, dict) or not set(required) <= set(body) <= set(required) | set(
        optional
    ):
        raise HTTPException(422, "robot_request_fields")
    return body


def install(app, store):
    library = RobotLibrary(store)

    @app.get("/owner", response_class=HTMLResponse)
    def owner():
        return HTMLResponse(
            (ASSETS / "index.html").read_text(encoding="utf-8"),
            headers={
                "Content-Security-Policy": "default-src 'none'; "
                "script-src 'self'; style-src 'self'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/owner.js")
    def script():
        return Response((ASSETS / "owner.js").read_bytes(), media_type="text/javascript")

    @app.get("/owner.css")
    def style():
        return Response((ASSETS / "owner.css").read_bytes(), media_type="text/css")

    @app.get("/robots")
    def robots():
        return library.status()

    @app.post("/robots/save")
    async def save(request: Request):
        body = await fields(request, (), ("source", "name", "note"))
        return {"saved": await asyncio.to_thread(library.save_copy, now(), **body)}

    @app.post("/robots/blank")
    def blank():
        return {"saved": library.add_blank(now())}

    @app.post("/robots/import")
    async def imported(request: Request):
        raw = await read_body(request, MAX_ARCHIVE)
        return {
            "saved": await asyncio.to_thread(library.import_archive, raw, now()),
            "active_changed": False,
        }

    @app.get("/robots/{identifier}/export")
    def exported(identifier: str):
        return Response(
            library.export(identifier),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="robot.pipmind"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/robots/{identifier}/favorite")
    async def favorite(identifier: str, request: Request):
        body = await fields(request, ("favorite",))
        await asyncio.to_thread(library.favorite, identifier, body["favorite"], now())
        return {"favorite": body["favorite"]}

    @app.post("/robots/{identifier}/preview")
    def preview(identifier: str):
        return library.preview(identifier, now())

    @app.post("/robots/load")
    async def load(request: Request):
        body = await fields(request, ("preview_id", "target_sha256", "confirm"))
        if body["confirm"] is not True:
            raise HTTPException(422, "robot_confirmation_required")
        return await asyncio.to_thread(
            library.load, body["preview_id"], body["target_sha256"], now()
        )

    @app.post("/robots/auto-learn")
    async def auto_learn(request: Request):
        body = await fields(request, ("enabled",))
        return await asyncio.to_thread(library.set_auto_learn, body["enabled"], now())

    @app.post("/robots/retention")
    async def retention(request: Request):
        body = await fields(request, ("days", "count"))
        await asyncio.to_thread(library.retention, body["days"], body["count"], now())
        return {"saved": True}

    @app.post("/robots/cleanup")
    def cleanup():
        return {"removed": library.cleanup(now())}

    @app.post("/robots/pause")
    async def pause(request: Request):
        body = await fields(request, ("paused", "confirm"))
        if type(body["paused"]) is not bool or body["confirm"] is not True:
            raise ShadowError("robot_confirmation_required")
        await asyncio.to_thread(store.set_paused, body["paused"])
        return {"paused": body["paused"], "mode": "shadow", "dispatches": 0}
