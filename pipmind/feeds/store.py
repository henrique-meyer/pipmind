"""Durable isolated development inbox/outbox. Hosted SQL is a separate M4 adapter."""

import json
import sqlite3
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from uuid import uuid4

from pipmind.contracts import Contracts, event_facts, loads, stamp, validate_event
from pipmind.feeds.common import FeedError, identity

KINDS = {"macro": "ReleaseEvent", "news": "NewsEvent", "social": "PostEvent"}


@cache
def contracts():
    return Contracts()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def merge_state(store, db, patch):
    if patch is not None:
        key, values = patch
        current = store.state(key, {}, db=db)
        if not isinstance(current, dict) or not isinstance(values, dict):
            raise FeedError("invalid_feed_state_patch")
        store.save(key, {**current, **values}, db=db)


class FeedStore:
    def __init__(self, path, *, mode="local-read", profile="synthetic"):
        if str(path) == ":memory:" or mode not in {"local-read", "fixture"}:
            raise FeedError("durable_local_feed_store_required")
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise FeedError("incompatible_feed_store")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            binding = {"mode": mode, "profile": identity(profile)}
            existing = dict(db.execute("SELECT key,value FROM metadata"))
            if existing and existing != binding:
                raise FeedError("feed_store_profile_mismatch")
            db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", binding.items())
            db.execute(
                "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                sequence INTEGER PRIMARY KEY, producer TEXT NOT NULL, key TEXT UNIQUE NOT NULL,
                fingerprint TEXT NOT NULL, raw TEXT NOT NULL, received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending')""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, producer TEXT NOT NULL, key TEXT UNIQUE NOT NULL,
                facts TEXT NOT NULL, envelope TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS alerts (
                sequence INTEGER PRIMARY KEY, producer TEXT NOT NULL, code TEXT NOT NULL,
                ts TEXT NOT NULL)""")
            db.execute("PRAGMA user_version=1")
            if self.state("delivery:source_id", db=db) is None:
                self.save("delivery:source_id", str(uuid4()), db=db)

    def source_id(self):
        return self.state("delivery:source_id")

    def reprocess(self, key):
        with self.transaction() as db:
            result = db.execute(
                "UPDATE inbox SET status='pending' WHERE key=? AND status='quarantined'", (key,)
            )
            return result.rowcount == 1

    def capture(self, key):
        with self.transaction() as db:
            row = db.execute(
                "SELECT raw,received_at,status FROM inbox WHERE key=?", (key,)
            ).fetchone()
            return {"raw": loads(row[0]), "received_at": row[1], "status": row[2]} if row else None

    @contextmanager
    def transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except sqlite3.Error:
            if db and db.in_transaction:
                db.execute("ROLLBACK")
            raise FeedError("feed_store_unavailable") from None
        except Exception:
            if db and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db:
                db.close()

    def state(self, key, default=None, *, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.state(key, default, db=connection)
        row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return loads(row[0]) if row else default

    def save(self, key, value, *, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.save(key, value, db=connection)
        db.execute(
            "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded(value)),
        )

    def claim(self, key, now, interval):
        with self.transaction() as db:
            if self.state(key, 0, db=db) > now.timestamp():
                return False
            self.save(key, now.timestamp() + interval, db=db)
            return True

    def alert(self, producer, code, now):
        with self.transaction() as db:
            db.execute("INSERT INTO alerts VALUES (NULL,?,?,?)", (producer, code, stamp(now)))
            db.execute(
                "DELETE FROM alerts WHERE sequence <= (SELECT MAX(sequence)-1000 FROM alerts)"
            )

    def ingest(
        self, producer, key, raw, fingerprint, received, *, cursor=None, duplicate_facts=None
    ):
        if producer not in KINDS or len(encoded(raw).encode()) > 262_144 or len(key) > 512:
            raise FeedError("invalid_feed_inbox")
        with self.transaction() as db:
            row = db.execute("SELECT fingerprint,raw FROM inbox WHERE key=?", (key,)).fetchone()
            if row and (
                row[0] != fingerprint
                or (
                    duplicate_facts is not None
                    and encoded(duplicate_facts(loads(row[1]))) != encoded(duplicate_facts(raw))
                )
            ):
                raise FeedError("conflicting_feed_delivery")
            if not row:
                db.execute(
                    "INSERT INTO inbox VALUES (NULL,?,?,?,?,?,'pending')",
                    (producer, key, fingerprint, encoded(raw), stamp(received)),
                )
            if cursor:
                self.save(*cursor, db=db)
        return row is not None

    def pending(self, producer):
        with self.transaction() as db:
            return [
                {"key": key, "raw": loads(raw), "received_at": ts}
                for key, raw, ts in db.execute(
                    "SELECT key,raw,received_at FROM inbox WHERE producer=? AND status='pending' "
                    "AND key NOT LIKE 'rest-page:%' "
                    "ORDER BY sequence LIMIT 100",
                    (producer,),
                )
            ]

    def emit_many(self, db, producer, payloads, available):
        for payload in payloads:
            self.emit(db, producer, payload, available)

    def emit(self, db, producer, payload, available):
        kind = KINDS[producer]
        envelope = {
            "schema_version": 1,
            "kind": kind,
            "message_id": payload["event_id"],
            "available_at": stamp(available),
            "payload": payload,
        }
        validate_event(envelope, contracts())
        key, facts = payload["event_id"], encoded(event_facts(envelope))
        old = db.execute("SELECT facts FROM events WHERE key=?", (key,)).fetchone()
        if old and old[0] != facts:
            raise FeedError("conflicting_normalized_event")
        if not old:
            db.execute(
                "INSERT INTO events VALUES (NULL,?,?,?,?)",
                (producer, key, facts, encoded(envelope)),
            )

    def complete(self, producer, key, payload, now):
        with self.transaction() as db:
            row = db.execute("SELECT status,producer FROM inbox WHERE key=?", (key,)).fetchone()
            if row != ("pending", producer):
                return
            self.emit(db, producer, payload, now)
            db.execute("UPDATE inbox SET status='complete' WHERE key=?", (key,))

    def quarantine(self, key):
        with self.transaction() as db:
            db.execute("UPDATE inbox SET status='quarantined' WHERE key=?", (key,))

    def complete_batch(self, producer, key, payloads, now, *, cursor=None, state_patch=None):
        with self.transaction() as db:
            row = db.execute("SELECT status,producer FROM inbox WHERE key=?", (key,)).fetchone()
            # Manual capture recovery emits events without moving checkpoints.
            # Normal polling may then initialize an absent cursor, never rewind one.
            if row == ("complete", producer) and cursor and self.state(cursor[0], db=db) is None:
                self.save(*cursor, db=db)
                merge_state(self, db, state_patch)
            if row != ("pending", producer):
                return
            for payload in payloads:
                self.emit(db, producer, payload, now)
            db.execute("UPDATE inbox SET status='complete' WHERE key=?", (key,))
            if cursor:
                self.save(*cursor, db=db)
            merge_state(self, db, state_patch)

    def page(self, producer, payloads, key, cursor, now):
        with self.transaction() as db:
            for payload in payloads:
                self.emit(db, producer, payload, now)
            self.save(key, cursor, db=db)

    def events(self, after=0, limit=100, *, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.events(after, limit, db=connection)
        return [
            {"sequence": seq, "producer": producer, "event": loads(event)}
            for seq, producer, event in db.execute(
                "SELECT sequence,producer,envelope FROM events WHERE sequence>? "
                "ORDER BY sequence LIMIT ?",
                (after, limit),
            )
        ]

    def handoff(self, after=0, limit=100, *, include_calendar=False):
        """Same read frame as hosted SQL, without consuming or rewriting inputs."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise FeedError("invalid_event_page")
        with self.transaction() as db:
            frame = {
                "events": self.events(after, limit, db=db),
                "coverage": self.state("canonical:quality", {"status": "unverified"}, db=db),
                "quarantined": db.execute(
                    "SELECT COUNT(*) FROM inbox WHERE status='quarantined'"
                ).fetchone()[0],
            }
            if include_calendar:
                frame["calendar"] = self.state("calendar:data", db=db)
            return frame

    def status(self):
        with self.transaction() as db:
            return {
                "events": dict(
                    db.execute("SELECT producer,COUNT(*) FROM events GROUP BY producer")
                ),
                "alerts": [
                    {"producer": p, "code": c, "ts": ts}
                    for p, c, ts in db.execute(
                        "SELECT producer,code,ts FROM alerts ORDER BY sequence DESC LIMIT 100"
                    )
                ],
                "quarantined": db.execute(
                    "SELECT COUNT(*) FROM inbox WHERE status='quarantined'"
                ).fetchone()[0],
            }
