"""Retained schedule evidence, distinct from actual releases and model working state."""

from pipmind.contracts import utc
from pipmind.feeds.macro import calendar_date
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.store import metadata


def retain_calendar(store, cached, moment):
    if not cached:
        return {"status": "unavailable"}
    if not isinstance(cached, dict) or not isinstance(cached.get("rows"), list):
        return {"status": "invalid_snapshot"}
    try:
        if utc(cached["fetched_at"]) > moment:
            return {"status": "future_snapshot"}
        for row in cached["rows"]:
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) for key in ("date", "country", "title", "impact")
            ):
                return {"status": "invalid_snapshot"}
            calendar_date(row["date"])
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid_snapshot"}
    body = encoded(
        {
            "kind": "calendar-snapshot-v1",
            "source": "forexfactory",
            "publication_time": None,
            "scope": "Schedule and consensus; not actual releases.",
            "snapshot": cached,
        }
    )
    if len(cached["rows"]) > 5000 or len(body.encode()) > 1_048_576:
        return {"status": "snapshot_bound_exceeded"}
    identifier = "calendar:" + digest(body)
    with store.transaction() as cur:
        store._head(cur)
        cur.execute(
            "SELECT body FROM pm_records WHERE trader=%s AND id=%s", (store.trader, identifier)
        )
        row = cur.fetchone()
        if row is None:
            store._insert(
                cur, metadata(identifier, "memory", moment.isoformat(), store.trader), body
            )
        else:
            if row[0] != body:
                raise ShadowError("calendar_record_collision")
    return {
        "record_id": identifier,
        "fetched_at": cached["fetched_at"],
        "rows": len(cached["rows"]),
    }
