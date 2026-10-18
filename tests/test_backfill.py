from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import utc
from pipmind.feeds.backfill import CanonicalBackfill, normalize_page, reprocess_capture
from pipmind.feeds.common import FeedError
from pipmind.feeds.fixtures import configuration
from pipmind.feeds.macro import Macro
from pipmind.feeds.store import FeedStore, encoded

MOMENT = utc("2026-10-18T12:00:00Z")
MAPPING = configuration()["macro"][0]


def page(offset=0, more=False):
    return {
        "currency": "usd",
        "indicator": "synthetic_cpi",
        "data": [
            {
                "announcement_id": "synthetic-row-1",
                "date": "2026-09-30",
                "val": 3,
                "announcement_datetime": None,
                "publication_time_status": "unknown",
                "publication_time_precision": "unknown",
                "revisions": [
                    {
                        "val": 2.9,
                        "epoch": None,
                        "publication_time_status": "unknown",
                        "publication_time_precision": "unknown",
                        "collected_at_iso": "2026-10-05T09:00:00Z",
                    }
                ],
            }
        ],
        "pagination": {
            "offset": offset,
            "returned_count": 1,
            "has_more": more,
            "next_offset": offset + 1 if more else None,
        },
        "data_quality": {"known_source_gap": True},
    }


def test_canonical_expansion_preserves_unknown_times_and_distinct_vintages(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    calendar = SimpleNamespace(match=lambda *a: None)
    macro = Macro(store, calendar, [MAPPING], lambda: MOMENT)
    events = normalize_page(page(), MAPPING, macro, "2026-10-18T12:00:00Z")
    assert len(events) == 2 and len({x["event_id"] for x in events}) == 2
    assert {x["actual"] for x in events} == {2.9, 3}
    assert all(x["released_at"] is None for x in events)
    assert all(x["received_at"] == "2026-10-18T12:00:00Z" for x in events)


def test_canonical_page_restart_keeps_raw_capture_and_cursor_together(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    seen = []

    class Reads:
        def get(self, url, **kw):
            seen.append(url)
            payload = page(0, True) if "offset=0" in url else page(1, False)
            return SimpleNamespace(status=200, body=encoded(payload).encode(), headers={})

    calendar = SimpleNamespace(match=lambda *a: None)
    backfill = CanonicalBackfill(
        store, Reads(), "https://provider.invalid", "synthetic", calendar, lambda: MOMENT
    )
    assert backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    reopened = FeedStore(tmp_path / "feeds.db")
    backfill = CanonicalBackfill(
        reopened, Reads(), "https://provider.invalid", "synthetic", calendar, lambda: MOMENT
    )
    assert not backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert "offset=1" in seen[-1]
    assert len(reopened.events()) == 2
    with reopened.transaction() as db:
        raw = db.execute("SELECT raw FROM inbox WHERE key LIKE 'rest-page:%'").fetchall()
    assert len(raw) == 2 and all("known_source_gap" in row[0] for row in raw)


def test_canonical_metadata_and_pagination_fail_closed(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    calendar = SimpleNamespace(match=lambda *a: None)
    payload = page()
    payload["pagination"]["has_more"] = True
    payload["pagination"]["next_offset"] = 0
    reads = SimpleNamespace(
        get=lambda *a, **kw: SimpleNamespace(status=200, body=encoded(payload).encode())
    )
    backfill = CanonicalBackfill(
        store, reads, "https://provider.invalid", "synthetic", calendar, lambda: MOMENT
    )
    with pytest.raises(FeedError):
        backfill.page(MAPPING, "2026-01-01", "2026-10-18")
    assert store.events() == []
    wrong = deepcopy(page())
    wrong["indicator"] = "different_series"
    with pytest.raises(FeedError):
        normalize_page(
            wrong, MAPPING, Macro(store, calendar, [MAPPING], lambda: MOMENT), MOMENT.isoformat()
        )


def test_quarantined_canonical_capture_reprocesses_without_network_or_cursor_rewind(tmp_path):
    store = FeedStore(tmp_path / "feeds.db")
    key = "rest-page:synthetic-quarantine"
    store.ingest("macro", key, {"canonical_page": page()}, "synthetic", MOMENT)
    store.quarantine(key)
    store.save("canonical:sentinel", {"offset": 100})
    assert reprocess_capture(
        store, key, configuration(), SimpleNamespace(match=lambda *a: None), MOMENT
    )
    assert len(store.events()) == 2 and store.capture(key)["status"] == "complete"
    assert store.state("canonical:sentinel") == {"offset": 100}
    assert not reprocess_capture(store, key, configuration(), None, MOMENT)
