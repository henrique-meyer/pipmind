"""Bounded configuration, network reads and normalized feed errors."""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pipmind.contracts import ContractError, loads

MAX_BODY = 1_000_000


class FeedError(ContractError):
    """Fixed error codes only; upstream text and private values never become logs."""


def identity(*values):
    return hashlib.sha256("|".join(str(v) for v in values).encode()).hexdigest()


def url(value):
    try:
        parts = urlsplit(value)
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or re.search(r"\s|[\x00-\x1f]", value)
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or parts.port not in (None, 443)
        ):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise FeedError("invalid_feed_url") from None


def instant(value):
    from pipmind.contracts import utc

    try:
        if type(value) in (int, float):
            return datetime.fromtimestamp(value, UTC)
        if not isinstance(value, str):
            raise ValueError
        return utc(value)
    except (ValueError, OverflowError, OSError):
        raise FeedError("invalid_feed_timestamp") from None


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def markup(value):
    parser = Markup()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split()), parser.links


@dataclass
class ReadResult:
    status: int
    body: bytes
    headers: dict


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Reads:
    """GET only. Only owner-configured URLs and fixed provider paths reach this class."""

    def __init__(self, user_agent="PipMind/0.1 (private information-feed worker)"):
        self.opener, self.user_agent = build_opener(NoRedirect), user_agent

    def get(self, target, *, headers=None):
        request = Request(
            url(target),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, application/xml, text/xml, */*",
                **(headers or {}),
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise FeedError("feed_response_too_large")
                return ReadResult(response.status, body, dict(response.headers))
        except HTTPError as error:
            result = ReadResult(error.code, b"", dict(error.headers))
            error.close()
            return result
        except FeedError:
            raise
        except (OSError, URLError, ValueError):
            raise FeedError("feed_transport_unavailable") from None


def json_body(response):
    if response.status != 200:
        raise FeedError("feed_rate_limited" if response.status == 429 else "feed_read_failed")
    try:
        return loads(response.body)
    except (ContractError, RecursionError):
        raise FeedError("invalid_feed_json") from None
