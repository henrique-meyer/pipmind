"""Hosted SQL inbox/outbox implementing the existing feed worker store interface."""

from contextlib import contextmanager
from uuid import uuid4

from pipmind.contracts import event_facts, loads, stamp, validate_event
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.store import KINDS, contracts, encoded
from pipmind.shadow.common import ShadowError
from pipmind.shadow.sql import connector, transaction

DDL = (
    "IF OBJECT_ID('pf_meta') IS NULL CREATE TABLE pf_meta("
    "k varchar(64) COLLATE Latin1_General_100_BIN2 PRIMARY KEY,v nvarchar(max))",
    "IF OBJECT_ID('pf_state') IS NULL CREATE TABLE pf_state("
    "k varchar(512) COLLATE Latin1_General_100_BIN2 PRIMARY KEY,v nvarchar(max))",
    """IF OBJECT_ID('pf_inbox') IS NULL CREATE TABLE pf_inbox (
      seq bigint IDENTITY PRIMARY KEY, producer varchar(16) NOT NULL,
      k varchar(512) COLLATE Latin1_General_100_BIN2 UNIQUE NOT NULL, fingerprint char(64) NOT NULL,
      raw nvarchar(max) NOT NULL, received varchar(32) NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'pending')""",
    """IF OBJECT_ID('pf_events') IS NULL CREATE TABLE pf_events (
      seq bigint IDENTITY PRIMARY KEY, producer varchar(16) NOT NULL,
      k varchar(512) COLLATE Latin1_General_100_BIN2 UNIQUE NOT NULL, facts nvarchar(max) NOT NULL,
      envelope nvarchar(max) NOT NULL)""",
    """IF OBJECT_ID('pf_alerts') IS NULL CREATE TABLE pf_alerts (
      seq bigint IDENTITY PRIMARY KEY,producer varchar(16),code varchar(128),ts varchar(32))""",
)


class SqlFeedStore:
    def __init__(self, connect, *, mode="local-read", profile="synthetic"):
        if mode != "local-read":
            raise FeedError("sql_feed_requires_read_profile")
        self.connect = connect
        with self.transaction() as db:
            for statement in DDL:
                db.execute(statement)
            db.execute("SELECT k,v FROM pf_meta")
            meta = dict(db.fetchall())
            if meta and (meta.get("profile") != identity(profile) or meta.get("version") != "1"):
                raise FeedError("feed_store_profile_mismatch")
            if not meta:
                for key, value in (
                    ("profile", identity(profile)),
                    ("version", "1"),
                    ("source", str(uuid4())),
                ):
                    db.execute("INSERT pf_meta VALUES(%s,%s)", (key, value))

    @contextmanager
    def transaction(self):
        try:
            with transaction(self.connect, "pipmind-feed-store") as cur:
                yield cur
        except ShadowError:
            raise FeedError("feed_store_unavailable") from None

    def source_id(self):
        with self.transaction() as db:
            db.execute("SELECT v FROM pf_meta WHERE k='source'")
            return db.fetchone()[0]

    def state(self, key, default=None, *, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.state(key, default, db=connection)
        db.execute("SELECT v FROM pf_state WHERE k=%s", (key,))
        row = db.fetchone()
        return loads(row[0]) if row else default

    def save(self, key, value, *, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.save(key, value, db=connection)
        if len(key) > 512:
            raise FeedError("feed_state_key_size")
        db.execute("SELECT 1 FROM pf_state WHERE k=%s", (key,))
        if db.fetchone():
            db.execute("UPDATE pf_state SET v=%s WHERE k=%s", (encoded(value), key))
        else:
            db.execute("INSERT pf_state VALUES(%s,%s)", (key, encoded(value)))

    def claim(self, key, now, interval):
        with self.transaction() as db:
            if self.state(key, 0, db=db) > now.timestamp():
                return False
            self.save(key, now.timestamp() + interval, db=db)
            return True

    def alert(self, producer, code, now):
        with self.transaction() as db:
            db.execute(
                "INSERT pf_alerts(producer,code,ts) VALUES(%s,%s,%s)", (producer, code, stamp(now))
            )
            db.execute("DELETE FROM pf_alerts WHERE seq <= (SELECT MAX(seq)-1000 FROM pf_alerts)")

    def ingest(self, producer, key, raw, fingerprint, received, *, cursor=None):
        if producer not in KINDS or len(encoded(raw).encode()) > 262144 or len(key) > 512:
            raise FeedError("invalid_feed_inbox")
        with self.transaction() as db:
            db.execute("SELECT fingerprint FROM pf_inbox WHERE k=%s", (key,))
            old = db.fetchone()
            if old and old[0] != fingerprint:
                raise FeedError("conflicting_feed_delivery")
            if not old:
                db.execute(
                    "INSERT pf_inbox(producer,k,fingerprint,raw,received) VALUES(%s,%s,%s,%s,%s)",
                    (producer, key, fingerprint, encoded(raw), stamp(received)),
                )
            if cursor:
                self.save(*cursor, db=db)
            return old is not None

    def pending(self, producer):
        with self.transaction() as db:
            db.execute(
                "SELECT TOP(100) k,raw,received FROM pf_inbox WHERE producer=%s "
                "AND status='pending' AND k NOT LIKE 'rest-page:%' ORDER BY seq",
                (producer,),
            )
            return [
                {"key": key, "raw": loads(raw), "received_at": received}
                for key, raw, received in db.fetchall()
            ]

    def emit(self, db, producer, payload, available):
        envelope = {
            "schema_version": 1,
            "kind": KINDS[producer],
            "message_id": payload["event_id"],
            "available_at": stamp(available),
            "payload": payload,
        }
        validate_event(envelope, contracts())
        facts = encoded(event_facts(envelope))
        db.execute("SELECT facts FROM pf_events WHERE k=%s", (payload["event_id"],))
        old = db.fetchone()
        if old and old[0] != facts:
            raise FeedError("conflicting_normalized_event")
        if not old:
            db.execute(
                "INSERT pf_events(producer,k,facts,envelope) VALUES(%s,%s,%s,%s)",
                (producer, payload["event_id"], facts, encoded(envelope)),
            )

    def complete(self, producer, key, payload, now):
        with self.transaction() as db:
            db.execute("SELECT status,producer FROM pf_inbox WHERE k=%s", (key,))
            row = db.fetchone()
            if not row or tuple(row) != ("pending", producer):
                return
            self.emit(db, producer, payload, now)
            db.execute("UPDATE pf_inbox SET status='complete' WHERE k=%s", (key,))

    def quarantine(self, key):
        with self.transaction() as db:
            db.execute("UPDATE pf_inbox SET status='quarantined' WHERE k=%s", (key,))

    def complete_batch(self, producer, key, payloads, now, *, cursor=None):
        with self.transaction() as db:
            db.execute("SELECT status,producer FROM pf_inbox WHERE k=%s", (key,))
            row = db.fetchone()
            if row and tuple(row) == ("complete", producer) and cursor:
                self.save(*cursor, db=db)
            if not row or tuple(row) != ("pending", producer):
                return
            for payload in payloads:
                self.emit(db, producer, payload, now)
            db.execute("UPDATE pf_inbox SET status='complete' WHERE k=%s", (key,))
            if cursor:
                self.save(*cursor, db=db)

    def reprocess(self, key):
        with self.transaction() as db:
            db.execute(
                "UPDATE pf_inbox SET status='pending' OUTPUT INSERTED.seq "
                "WHERE k=%s AND status='quarantined'",
                (key,),
            )
            return db.fetchone() is not None

    def capture(self, key):
        with self.transaction() as db:
            db.execute("SELECT raw,received,status FROM pf_inbox WHERE k=%s", (key,))
            row = db.fetchone()
            return {"raw": loads(row[0]), "received_at": row[1], "status": row[2]} if row else None

    def page(self, producer, payloads, key, cursor, now):
        with self.transaction() as db:
            for payload in payloads:
                self.emit(db, producer, payload, now)
            self.save(key, cursor, db=db)

    def events(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise FeedError("invalid_event_page")
        with self.transaction() as db:
            db.execute(
                "SELECT TOP(%s) seq,producer,envelope FROM pf_events WHERE seq>%s ORDER BY seq",
                (limit, after),
            )
            return [
                {"sequence": seq, "producer": producer, "event": loads(event)}
                for seq, producer, event in db.fetchall()
            ]

    def status(self):
        with self.transaction() as db:
            db.execute("SELECT producer,COUNT(*) FROM pf_events GROUP BY producer")
            counts = dict(db.fetchall())
            db.execute("SELECT TOP(100) producer,code,ts FROM pf_alerts ORDER BY seq DESC")
            alerts = [{"producer": p, "code": c, "ts": ts} for p, c, ts in db.fetchall()]
            db.execute("SELECT COUNT(*) FROM pf_inbox WHERE status='quarantined'")
            return {"events": counts, "alerts": alerts, "quarantined": db.fetchone()[0]}


def configured_store(path, *, mode="local-read", profile="synthetic"):
    import os

    from pipmind.feeds.store import FeedStore

    backend = os.environ.get("PIPMIND_FEED_BACKEND", "sqlite")
    if backend == "sql":
        return SqlFeedStore(connector(), mode=mode, profile=profile)
    if backend != "sqlite":
        raise FeedError("unknown_feed_backend")
    return FeedStore(path, mode=mode, profile=profile)
