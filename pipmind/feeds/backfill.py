"""Bounded canonical REST capture. Retained vintages never imply complete history."""

import re
from datetime import date
from urllib.parse import urlencode

from pipmind.contracts import ContractError
from pipmind.feeds.common import FeedError, identity, json_body, url
from pipmind.feeds.macro import Macro
from pipmind.feeds.store import encoded


def normalize_page(payload, mapping, macro, received):
    if (
        not isinstance(payload, dict)
        or str(payload.get("currency", "")).upper() != mapping["currency"]
        or payload.get("indicator") != mapping["indicator"]
        or not isinstance(payload.get("data"), list)
        or len(payload["data"]) > 100
    ):
        raise FeedError("invalid_canonical_page")
    events = []
    for row in payload["data"]:
        if not isinstance(row, dict) or not row.get("announcement_id"):
            raise FeedError("canonical_row_identity_required")
        vintages = row.get("revisions", [])
        if not isinstance(vintages, list) or len(vintages) > 100:
            raise FeedError("canonical_vintage_bound")
        for vintage in [*vintages, row]:
            if not isinstance(vintage, dict):
                raise FeedError("invalid_canonical_vintage")
            value = {**vintage, "date": row["date"]}
            if "epoch" in vintage:
                value["announcement_datetime"] = vintage["epoch"]
            if "previous_value" in value:
                value["previous"] = value["previous_value"]
            # Include provenance/availability fields in identity: equal values
            # with different capture clocks remain separate retained evidence.
            key = "rest-" + identity(
                mapping["currency"], mapping["indicator"], row["announcement_id"], encoded(value)
            )
            raw = {
                **value,
                "event_id": key,
                "currency": mapping["currency"],
                "indicator": mapping["indicator"],
                "records_written": 1,
            }
            events.append(macro.normalize(raw, received))
    if len(events) > 500:
        raise FeedError("canonical_expansion_bound")
    return events


class CanonicalBackfill:
    def __init__(self, store, reads, root, api_key, calendar, now):
        self.store, self.reads, self.root = store, reads, url(root).rstrip("/")
        self.api_key, self.calendar, self.now = api_key, calendar, now

    def latest(self, mapping):
        """Seed the latest published observation even outside the replay window.

        This is one retained row, never a claim of complete history. Normal
        window paging and live delivery continue independently.
        """
        if not re.fullmatch(r"[A-Z]{3}", mapping["currency"]) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,100}", mapping["indicator"]
        ):
            raise FeedError("invalid_canonical_mapping")
        marker = "canonical:bootstrap:" + identity(mapping["currency"], mapping["indicator"])
        if self.store.state(marker) is not None:
            return False
        target = (
            self.root
            + "/v1/announcements/"
            + mapping["currency"].lower()
            + "/"
            + mapping["indicator"]
        )
        payload = json_body(
            self.reads.get(
                target + "?" + urlencode({"limit": 1, "revisions": "latest"}),
                headers={"X-API-Key": self.api_key},
            )
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("data"), list)
            or len(payload["data"]) > 1
        ):
            raise FeedError("invalid_latest_observation")
        moment = self.now()
        key = "rest-page:" + identity(marker, encoded(payload))
        self.store.ingest(
            "macro", key, {"canonical_page": payload}, identity(encoded(payload)), moment
        )
        try:
            events = normalize_page(
                payload,
                mapping,
                Macro(
                    self.store,
                    self.calendar,
                    [mapping],
                    self.now,
                ),
                moment.isoformat(),
            )
            self.store.complete_batch(
                "macro",
                key,
                events,
                moment,
                cursor=(
                    marker,
                    {
                        "captured_at": moment.isoformat(),
                        "status": "latest_retained_row_only" if events else "latest_unavailable",
                        "rows": len(events),
                        "complete_history_proven": False,
                    },
                ),
            )
        except (ContractError, KeyError, TypeError):
            self.store.quarantine(key)
            raise
        return bool(events)

    def page(self, mapping, start, end):
        if not re.fullmatch(r"[A-Z]{3}", mapping["currency"]) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,100}", mapping["indicator"]
        ):
            raise FeedError("invalid_canonical_mapping")
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise FeedError("invalid_canonical_window")
        key = "canonical:" + identity(
            mapping["currency"],
            mapping["indicator"],
            start,
            end,
            int(self.now().timestamp()) // 3600,
        )
        cursor = self.store.state(key, {"offset": 0, "complete": False})
        if cursor["complete"]:
            return False
        offset = cursor["offset"]
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise FeedError("canonical_page_bound")
        params = {
            "start_date": start,
            "end_date": end,
            "offset": offset,
            "limit": 100,
            "revisions": "all",
        }
        target = (
            self.root
            + "/v1/announcements/"
            + mapping["currency"].lower()
            + "/"
            + mapping["indicator"]
        )
        response = self.reads.get(
            target + "?" + urlencode(params), headers={"X-API-Key": self.api_key}
        )
        payload = json_body(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise FeedError("invalid_canonical_page")
        pagination = payload.get("pagination", {})
        if (
            not isinstance(pagination, dict)
            or pagination.get("offset") != offset
            or type(pagination.get("has_more")) is not bool
            or pagination.get("returned_count") != len(payload.get("data", []))
        ):
            raise FeedError("invalid_canonical_pagination")
        more = pagination["has_more"]
        next_offset = pagination.get("next_offset")
        if more and (type(next_offset) is not int or next_offset <= offset):
            raise FeedError("invalid_canonical_pagination")
        moment = self.now()
        capture_key = "rest-page:" + identity(key, offset, encoded(payload))
        self.store.ingest(
            "macro", capture_key, {"canonical_page": payload}, identity(encoded(payload)), moment
        )
        try:
            macro = Macro(self.store, self.calendar, [mapping], self.now)
            events = normalize_page(payload, mapping, macro, moment.isoformat())
            self.store.complete_batch(
                "macro",
                capture_key,
                events,
                moment,
                cursor=(
                    key,
                    {
                        "offset": next_offset if more else offset,
                        "complete": not more,
                        "status": "retained_window_captured" if not more else "partial",
                        "quality": payload.get("data_quality", {}),
                        "captured_at": moment.isoformat(),
                    },
                ),
            )
            with self.store.transaction() as db:
                quality = self.store.state("canonical:quality", {}, db=db)
                quality[mapping["currency"] + ":" + mapping["indicator"]] = {
                    "status": "partial" if more else "retained_window_captured",
                    "quality": payload.get("data_quality", {}),
                    "checked_at": moment.isoformat(),
                    "complete_history_proven": False,
                }
                self.store.save("canonical:quality", quality, db=db)
        except (ContractError, KeyError, TypeError):
            self.store.quarantine(capture_key)
            self.store.alert("macro", "canonical_page_quarantined", moment)
            raise
        return more


def reprocess_capture(store, key, config, calendar, moment):
    """Re-normalize the retained raw page; no network and no cursor rewind."""
    capture = store.capture(key)
    if not capture or capture["status"] != "quarantined":
        return False
    payload = capture["raw"].get("canonical_page")
    if not isinstance(payload, dict):
        raise FeedError("invalid_canonical_capture")
    mappings = [
        m
        for m in config["macro"]
        if m["currency"] == str(payload.get("currency", "")).upper()
        and m["indicator"] == payload.get("indicator")
    ]
    if len(mappings) != 1:
        raise FeedError("canonical_mapping_required")
    macro = Macro(store, calendar, mappings, lambda: moment)
    events = normalize_page(payload, mappings[0], macro, capture["received_at"])
    if not store.reprocess(key):
        return False
    try:
        store.complete_batch("macro", key, events, moment)
    except Exception:
        store.quarantine(key)
        raise
    return True
