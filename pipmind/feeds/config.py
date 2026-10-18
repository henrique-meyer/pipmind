"""Private JSON recipes are data. Prose lookup instructions are never evaluated."""

import re
from pathlib import Path

from pipmind.contracts import loads
from pipmind.feeds.common import MAX_BODY, FeedError, url
from pipmind.feeds.store import encoded


def validate(config):
    try:
        if config["schema_version"] != 1 or set(config) != {
            "schema_version",
            "calendar_url",
            "macro",
            "news",
            "roles",
        }:
            raise ValueError
        url(config["calendar_url"])
        for channel in ("macro", "news", "roles"):
            if not isinstance(config[channel], list) or len(config[channel]) > 100:
                raise ValueError
        if len(encoded(config).encode()) > MAX_BODY:
            raise ValueError
        identities = set()
        for mapping in config["macro"]:
            if "bootstrap_latest" in mapping and type(mapping["bootstrap_latest"]) is not bool:
                raise ValueError
            if not re.fullmatch(r"[A-Z]{3}", mapping["currency"]) or not re.fullmatch(
                r"[a-z_0-9]+", mapping["indicator"]
            ):
                raise ValueError
            key = (mapping["currency"], mapping["indicator"])
            if key in identities:
                raise ValueError
            identities.add(key)
            for field in ("name", "unit"):
                if not isinstance(mapping[field], str) or not mapping[field]:
                    raise ValueError
            if mapping["ff_title"] is not None and (
                not isinstance(mapping["ff_title"], str) or not mapping["ff_title"]
            ):
                raise ValueError
            if mapping["value_field"] not in {
                "val",
                "change",
                "val_mom",
                "pct_change_mom",
                "pct_change_yoy",
            }:
                raise ValueError
            for field in ("value_scale", "ff_scale"):
                if (
                    type(mapping[field]) not in (int, float)
                    or not 0 < mapping[field] <= 1_000_000_000
                ):
                    raise ValueError
        identities = set()
        for source in config["news"]:
            if any(
                key in source and type(source[key]) is not bool
                for key in ("allow_summary", "paywalled")
            ):
                raise ValueError
            if not re.fullmatch(r"[a-z0-9_-]{1,64}", source["id"]) or source["id"] in identities:
                raise ValueError
            identities.add(source["id"])
            url(source["url"])
            if (
                source["kind"] not in {"breaking", "analysis"}
                or source.get("interval_seconds", 300) < 60
            ):
                raise ValueError
        identities = set()
        for role in config["roles"]:
            if role.get("official_profile_url"):
                url(role["official_profile_url"])
            if "primary_allow_summary" in role and type(role["primary_allow_summary"]) is not bool:
                raise ValueError
            if not re.fullmatch(r"[a-z0-9_-]{1,64}", role["id"]) or role["id"] in identities:
                raise ValueError
            identities.add(role["id"])
            if type(role["priority"]) is not int or role["priority"] not in {1, 2, 3}:
                raise ValueError
            for recipe in role.get("lookups", []):
                url(recipe["url"])
                if not recipe["start"] or not recipe["end"]:
                    raise ValueError
            if role.get("lookups"):
                url(role["profile_url"])
            if role.get("primary_feed_url"):
                url(role["primary_feed_url"])
            for account in role["accounts"]:
                if (
                    not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account["handle"])
                    or account["kind"] not in {"personal", "institutional"}
                    or account["poll"]
                    not in {"full_timeline", "event_window", "rss_only", "backup"}
                ):
                    raise ValueError
                if "user_id" in account or "primary_down" in account:
                    raise ValueError  # IDs/backup state require verified runtime evidence.
                words = role.get(
                    "institutional_words" if account["kind"] == "institutional" else "role_words"
                )
                if (
                    not isinstance(words, list)
                    or not words
                    or any(not isinstance(w, str) or not w for w in words)
                ):
                    raise ValueError
        return config
    except (KeyError, TypeError, ValueError):
        raise FeedError("invalid_feed_configuration") from None


def load(path):
    if Path(path).stat().st_size > MAX_BODY:
        raise FeedError("feed_configuration_too_large")
    return validate(loads(Path(path).read_bytes()))
