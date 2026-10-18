"""FXMacroData durable notifications, publication evidence and hourly consensus cache."""

import hashlib
import hmac
import math
import re
from datetime import UTC, datetime

from pipmind.contracts import ContractError, loads, stamp
from pipmind.feeds.common import FeedError, identity, instant, json_body
from pipmind.feeds.store import encoded


def numeric(value, scale=1):
    if value is None or value in ("", "N/A", "--", "-"):
        return None
    if isinstance(value, bool):
        raise FeedError("invalid_macro_number")
    try:
        text = str(value).replace(",", "").strip()
        match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)([%KMB]?)", text, re.I)
        if not match:
            raise ValueError
        factor = {"": 1, "%": 1, "K": 1000, "M": 1_000_000, "B": 1_000_000_000}[match[2].upper()]
        result = float(match[1]) * factor * scale
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        raise FeedError("invalid_macro_number") from None


def calendar_date(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise FeedError("invalid_calendar_date") from None


class Calendar:
    def __init__(self, store, reads, target, now):
        self.store, self.reads, self.target, self.now = store, reads, target, now

    def refresh(self):
        now = self.now()
        if not self.store.claim("calendar:next_attempt", now, 3600):
            return self.rows()
        cached = self.store.state("calendar:data", {})
        headers = {"If-None-Match": cached["etag"]} if cached.get("etag") else {}
        try:
            response = self.reads.get(self.target, headers=headers)
            if response.status == 304 and cached.get("rows") is not None:
                return cached["rows"]
            rows = json_body(response)
            if not isinstance(rows, list) or len(rows) > 5000:
                raise FeedError("invalid_calendar")
            for row in rows:
                if not isinstance(row, dict):
                    raise FeedError("invalid_calendar")
                if not all(
                    isinstance(row.get(k), str) for k in ("title", "country", "date", "impact")
                ):
                    raise FeedError("invalid_calendar")
                calendar_date(row["date"])
            self.store.save(
                "calendar:data",
                {"rows": rows, "fetched_at": stamp(now), "etag": response.headers.get("ETag")},
            )
        except (FeedError, KeyError, TypeError):
            self.store.alert("macro", "calendar_refresh_failed", now)
        return self.rows()

    def rows(self):
        return self.store.state("calendar:data", {}).get("rows", [])

    def match(self, mapping, scheduled):
        matches = [
            row
            for row in self.rows()
            if row["country"].upper() == mapping["currency"]
            and row["title"] == mapping["ff_title"]
            and scheduled is not None
            and calendar_date(row["date"]) == scheduled
        ]
        if len(matches) > 1:
            raise FeedError("ambiguous_calendar_match")
        return matches[0] if matches else None


def announcement(data):
    if not isinstance(data, dict):
        raise FeedError("invalid_macro_notification")
    try:
        key = data["event_id"]
        if not isinstance(key, str) or not re.fullmatch(r"[\w.:-]{1,256}", key):
            raise ValueError
        if not re.fullmatch(r"[a-zA-Z]{3}", data["currency"]):
            raise ValueError
        if not re.fullmatch(r"[a-z_0-9]{1,100}", data["indicator"]):
            raise ValueError
        row = {
            **data.get("latest_announcement", {}),
            **{
                k: v
                for k, v in data.items()
                if k not in {"latest_announcement", "event_id", "currency", "indicator"}
            },
        }
        if not isinstance(row.get("date"), str) or len(row["date"]) > 32:
            raise ValueError
        numeric(row.get("val"))
        return key, row
    except (KeyError, ValueError, TypeError):
        raise FeedError("invalid_macro_notification") from None


def accept(store, data, now, *, stream_id=None):
    key, row = announcement(data)
    if stream_id is not None and stream_id != key:
        raise FeedError("sse_identity_mismatch")
    facts = {
        "currency": data["currency"].upper(),
        "indicator": data["indicator"],
        "date": row["date"],
        "val": row.get("val"),
        "announcement_datetime": row.get("announcement_datetime"),
    }
    return store.ingest(
        "macro",
        "fxmd:" + key,
        data,
        identity(encoded(facts)),
        now,
        cursor=("fxmd:last_event_id", key) if stream_id is not None else None,
    )


def webhook(store, headers, body, secret, now):
    headers = {k.lower(): v for k, v in headers.items()}
    try:
        ts, key = headers["x-fxmd-timestamp"], headers["x-fxmd-event-id"]
        signature = headers["x-fxmd-signature"]
        if not re.fullmatch(r"\d{1,12}", ts) or abs(now.timestamp() - int(ts)) > 300:
            raise FeedError("stale_webhook")
        if not secret or len(body) > 262_144 or not re.fullmatch(r"v1=[0-9a-f]{64}", signature):
            raise FeedError("invalid_webhook_signature")
        digest = hmac.new(
            secret.encode(), ts.encode() + b"." + key.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature[3:], digest):
            raise FeedError("invalid_webhook_signature")
        payload = loads(body)
        if (
            payload["event"] != "announcement"
            or payload["id"] != key
            or payload["data"]["event_id"] != key
        ):
            raise FeedError("webhook_identity_mismatch")
        return accept(store, payload["data"], now)
    except (KeyError, TypeError, UnicodeError):
        raise FeedError("invalid_webhook") from None


class Macro:
    def __init__(self, store, calendar, mappings, now):
        self.store, self.calendar, self.mappings, self.now = store, calendar, mappings, now

    def normalize(self, raw, received):
        key, row = announcement(raw)
        if raw.get("records_written", 1) != 1:
            raise FeedError("macro_batch_requires_backfill")
        mapping = next(
            (
                m
                for m in self.mappings
                if m["currency"] == raw["currency"].upper() and m["indicator"] == raw["indicator"]
            ),
            None,
        )
        if mapping is None:
            raise FeedError("unmapped_macro_indicator")
        publication = row.get("announcement_datetime")
        status = row.get(
            "publication_time_status", "unverified" if publication is not None else "unknown"
        )
        if status == "unknown":
            publication = None
        scheduled = row.get("official_planned_release_datetime")
        scheduled = instant(scheduled) if scheduled is not None else None
        calendar = self.calendar.match(mapping, scheduled)
        forecast = numeric(calendar.get("forecast"), mapping["ff_scale"]) if calendar else None
        forecast_source = "forexfactory" if forecast is not None else None
        if (
            forecast is None
            and mapping.get("forecast_field") == mapping["value_field"]
            and (
                row.get("forecast_available") is True and row.get("forecast_is_pre_release") is True
            )
        ):
            forecast = numeric(row.get("forecast"), mapping["value_scale"])
            forecast_source = "fxmacrodata" if forecast is not None else None
        result = {
            "schema_version": 1,
            "event_id": "fxmd:" + key,
            "currency": mapping["currency"],
            "indicator": mapping["name"],
            "actual": numeric(row.get(mapping["value_field"]), mapping["value_scale"]),
            "forecast": forecast,
            "previous": numeric(
                row.get(mapping.get("previous_field", "previous")), mapping["value_scale"]
            ),
            "period": row["date"],
            "unit": mapping["unit"],
            "source": "fxmacrodata",
            "scheduled_at": stamp(scheduled) if scheduled else None,
            "released_at": stamp(instant(publication)) if publication is not None else None,
            "publication_time_status": status,
            "publication_time_precision": row.get("publication_time_precision", "unknown"),
            "received_at": received,
        }
        if forecast_source:
            result["forecast_source"] = forecast_source
        if calendar and calendar["impact"].lower() in {"high", "medium", "low"}:
            result["impact"] = calendar["impact"].lower()
        if raw.get("update_of_event_id"):
            result["revision_of"] = "fxmd:" + raw["update_of_event_id"]
        return result

    def drain(self):
        for pending in self.store.pending("macro"):
            if "canonical_page" in pending["raw"]:
                continue  # Canonical pages commit with their own pagination cursor.
            try:
                result = self.normalize(pending["raw"], pending["received_at"])
                self.store.complete("macro", pending["key"], result, self.now())
            except (ContractError, KeyError, TypeError) as error:
                self.store.alert(
                    "macro",
                    str(error) if isinstance(error, FeedError) else "invalid_macro_row",
                    self.now(),
                )
                self.store.quarantine(pending["key"])
