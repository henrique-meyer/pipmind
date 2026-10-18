"""Bounded W3C event-stream parser and GET-only FXMacroData connector."""

import time
from urllib.parse import urlencode
from urllib.request import Request

from pipmind.contracts import loads
from pipmind.feeds.common import FeedError
from pipmind.feeds.macro import accept


class SSE:
    def __init__(self):
        self.fields, self.size = {}, 0

    def line(self, raw):
        self.size += len(raw)
        if self.size > 262_144:
            raise FeedError("sse_frame_too_large")
        try:
            line = raw.decode("utf-8-sig").rstrip("\r\n")
        except UnicodeError:
            raise FeedError("invalid_sse_encoding") from None
        if line == "":
            fields, self.fields, self.size = self.fields, {}, 0
            if fields.get("event") == "announcement" and fields.get("data"):
                return fields.get("id"), loads("\n".join(fields["data"]))
            return None
        if line.startswith(":"):
            self.size -= len(raw)
            return None
        key, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if key == "data":
            self.fields.setdefault(key, []).append(value)
        elif key in {"id", "event"}:
            if "\x00" in value:
                raise FeedError("invalid_sse_identity")
            self.fields[key] = value
        return None


def stream_once(store, reads, api_root, api_key, mappings, now, stop):
    filters = {
        "payload": "full",
        "currencies": ",".join(sorted({m["currency"].lower() for m in mappings})),
        "indicators": ",".join(sorted({m["indicator"] for m in mappings})),
    }
    headers = {"X-API-Key": api_key, "Accept": "text/event-stream", "User-Agent": reads.user_agent}
    cursor = store.state("fxmd:last_event_id")
    if cursor:
        headers["Last-Event-ID"] = cursor
    request = Request(
        api_root.rstrip("/") + "/v1/stream/events?" + urlencode(filters),
        headers=headers,
        method="GET",
    )
    try:
        with reads.opener.open(request, timeout=25) as response:
            if response.status != 200 or not response.headers.get("Content-Type", "").startswith(
                "text/event-stream"
            ):
                raise FeedError("invalid_sse_response")
            parser, started = SSE(), time.monotonic()
            while not stop.is_set() and time.monotonic() - started < 60:
                line = response.readline(262_145)
                if not line:
                    return
                parsed = parser.line(line)
                if parsed:
                    stream_id, data = parsed
                    if not stream_id:
                        raise FeedError("missing_sse_identity")
                    accept(store, data, now(), stream_id=stream_id)
    except FeedError:
        raise
    except Exception:
        raise FeedError("macro_stream_disconnected") from None
