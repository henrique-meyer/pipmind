"""The compact profile is deliberately distinct from retained experience."""

import io
import zipfile

import pytest

from pipmind.portability import BundleError, validate_bundle
from pipmind.shadow.common import ShadowError, encoded
from pipmind.shadow.robot import blank_content, content_hash, export_robot, import_robot


def repack(raw, mutate):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = [(entry.filename, archive.read(entry)) for entry in archive.infolist()]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in mutate(entries):
            archive.writestr(name, body)
    return output.getvalue()


def test_exact_unicode_round_trip_and_no_account_or_owner_authority():
    content = {
        **blank_content(),
        "guides": "# Dated guide\nnaïve ≠ unsafe\r\n",
        "lessons": "Prior rules failed; preserve contrary evidence.\n",
    }
    raw = export_robot(content)
    assert import_robot(raw) == content
    assert content_hash(import_robot(raw)) == content_hash(content)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert set(archive.namelist()) == {"manifest.json", *(key + ".md" for key in content)}
    with pytest.raises(BundleError):
        validate_bundle(raw)  # Never accepted as an experience/recovery archive.


@pytest.mark.parametrize(
    "mutation",
    [
        lambda entries: entries + [("../escape.md", b"unsafe")],
        lambda entries: entries + [entries[1]],
        lambda entries: entries[:-1],
        lambda entries: [("C:\\escape.md", b"x"), *entries[1:]],
        lambda entries: [
            (name, b"changed" if name == "identity.md" else body) for name, body in entries
        ],
        lambda entries: [
            (name, b"x" * 32769 if name == "lessons.md" else body) for name, body in entries
        ],
        lambda entries: [
            (name, b"\xff" if name == "guides.md" else body) for name, body in entries
        ],
        lambda entries: [
            (name, encoded({"auto_learn": True}).encode() if name == "manifest.json" else body)
            for name, body in entries
        ],
    ],
)
def test_incomplete_corrupt_extra_and_unsafe_archives_are_refused(mutation):
    with pytest.raises(ShadowError, match="robot_invalid_archive"):
        import_robot(repack(export_robot(blank_content()), mutation))


def test_symlink_and_oversized_archive_refused():
    original = export_robot(blank_content())
    with zipfile.ZipFile(io.BytesIO(original)) as source:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for entry in source.infolist():
                if entry.filename == "identity.md":
                    entry.create_system = 3
                    entry.external_attr = 0o120777 << 16
                target.writestr(entry, source.read(entry.filename))
    with pytest.raises(ShadowError):
        import_robot(output.getvalue())
    with pytest.raises(ShadowError, match="robot_archive_size"):
        import_robot(b"x" * 524289)


def test_compact_profile_never_accepts_owner_settings_or_silent_truncation():
    for content in (
        {**blank_content(), "auto_learn": True},
        {**blank_content(), "lessons": "é" * 16385},
    ):
        with pytest.raises(ShadowError, match="robot_invalid_content"):
            export_robot(content)
