"""Private shadow data is never included in exception messages."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pipmind.contracts import ContractError, loads


class ShadowError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def now():
    return datetime.now(UTC)


def terms(text):
    return sorted(set(re.findall(r"[a-z0-9]{2,40}", text.casefold())))[:64]


def bounded_json(raw, limit=262144):
    if len(raw.encode() if isinstance(raw, str) else raw) > limit:
        raise ShadowError("shadow_payload_size")
    try:
        return loads(raw)
    except ContractError:
        raise ShadowError("shadow_invalid_json") from None


def read_private(path, limit):
    with Path(path).open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ShadowError("shadow_file_size")
    return content
