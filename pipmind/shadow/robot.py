"""Compact robot-only archives. Text is data; no archive path is extracted."""

import io
import re
import zipfile
import zlib
from copy import deepcopy
from uuid import uuid4

from pipmind.contracts import ROOT, ContractError, Contracts, loads
from pipmind.shadow.common import ShadowError, digest, encoded, now

MAX_ARCHIVE = 524288
MAX_CONTENT = 131072
COMPATIBILITY = "pipmind-robot-v1"


def validate_content(content):
    try:
        Contracts().validate("robot-content", content)
        if len(encoded(content).encode()) > MAX_CONTENT:
            raise ValueError
        for key in ("identity", "playbook", "guides", "lessons"):
            if len(content[key].encode()) > 32768 or "\x00" in content[key]:
                raise ValueError
        if not content["identity"].strip() or not content["playbook"].strip():
            raise ValueError
    except (ContractError, TypeError, ValueError, KeyError, RecursionError):
        raise ShadowError("robot_invalid_content") from None
    return deepcopy(content)


def content_hash(content):
    return digest(encoded(validate_content(content)))


def blank_content():
    folder = ROOT / "templates" / "blank"
    return validate_content(
        {
            "identity": (folder / "identity.md").read_text(encoding="utf-8"),
            "playbook": (folder / "playbook.md").read_text(encoding="utf-8"),
            "guides": "",
            "lessons": "",
        }
    )


def export_robot(content, source=None):
    content = validate_content(content)
    files = {key + ".md": value.encode() for key, value in content.items()}
    manifest = {
        "format": "pipmind",
        "schema_version": 3,
        "profile": "robot-save",
        "compatibility": COMPATIBILITY,
        "content_sha256": content_hash(content),
        "export_id": str(uuid4()),
        "created_at": now().isoformat().replace("+00:00", "Z"),
        "source": source,
        "files": {path: {"sha256": digest(raw), "bytes": len(raw)} for path, raw in files.items()},
    }
    Contracts().validate("robot-save-manifest", manifest)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", encoded(manifest))
        for path, raw in sorted(files.items()):
            archive.writestr(path, raw)
    return result.getvalue()


def import_robot(raw, *, with_manifest=False):
    if not isinstance(raw, bytes) or len(raw) > MAX_ARCHIVE:
        raise ShadowError("robot_archive_size")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            expected = {"manifest.json", "identity.md", "playbook.md", "guides.md", "lessons.md"}
            if (
                len(entries) != 5
                or {entry.filename for entry in entries} != expected
                or sum(entry.file_size for entry in entries) > MAX_CONTENT + 8192
                or archive.comment
            ):
                raise ValueError
            files = {}
            for entry in entries:
                mode = entry.external_attr >> 16
                if (
                    entry.flag_bits & 1
                    or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or mode & 0o170000 not in {0, 0o100000}
                    or entry.extra
                    or entry.orig_filename != entry.filename
                    or entry.file_size > (8192 if entry.filename == "manifest.json" else 32768)
                ):
                    raise ValueError
                with archive.open(entry) as stream:
                    files[entry.filename] = stream.read(entry.file_size + 1)
                if len(files[entry.filename]) != entry.file_size:
                    raise ValueError
        manifest = loads(files.pop("manifest.json"))
        Contracts().validate("robot-save-manifest", manifest)
        if (
            set(manifest)
            != {
                "format",
                "schema_version",
                "profile",
                "compatibility",
                "content_sha256",
                "files",
                "export_id",
                "created_at",
                "source",
            }
            or manifest["format"] != "pipmind"
            or manifest["schema_version"] != 3
            or manifest["profile"] != "robot-save"
            or manifest["compatibility"] != COMPATIBILITY
            or manifest["files"]
            != {path: {"sha256": digest(body), "bytes": len(body)} for path, body in files.items()}
        ):
            raise ValueError
        content = validate_content(
            {path[:-3]: body.decode("utf-8") for path, body in files.items()}
        )
        if manifest["content_sha256"] != content_hash(content):
            raise ValueError
        return (content, manifest) if with_manifest else content
    except (
        ShadowError,
        ContractError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,
        RuntimeError,
        RecursionError,
    ):
        raise ShadowError("robot_invalid_archive") from None


def label(value, limit):
    if not isinstance(value, str) or len(value) > limit or re.search(r"[\x00-\x1f]", value):
        raise ShadowError("robot_invalid_label")
    return value
