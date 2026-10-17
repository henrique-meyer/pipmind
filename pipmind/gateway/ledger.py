"""Transactional SQLite ledger for isolated dry development, never hosted execution."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from pipmind.contracts import Contracts, loads, stamp
from pipmind.gateway.config import GatewayError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DryLedger:
    def __init__(self, path, *, profile="synthetic", contracts=None):
        self.path, self.contracts = str(path), contracts or Contracts()
        if self.path == ":memory:":
            raise GatewayError("durable_dry_ledger_required")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise GatewayError("incompatible_dry_ledger")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            binding = hashlib.sha256(profile.encode()).hexdigest()
            expected = {"mode": "dry-only-v1", "profile": binding}
            existing = dict(db.execute("SELECT key, value FROM metadata"))
            if existing and existing != expected:
                raise GatewayError("dry_ledger_profile_mismatch")
            db.executemany("INSERT OR IGNORE INTO metadata VALUES (?, ?)", expected.items())
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY, request TEXT NOT NULL, acknowledgement TEXT,
                state TEXT NOT NULL CHECK(state IN ('not_sent', 'refused', 'uncertain')),
                broker_id TEXT UNIQUE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS journal (
                sequence INTEGER PRIMARY KEY, command_id TEXT NOT NULL,
                kind TEXT NOT NULL, ts TEXT NOT NULL, code TEXT NOT NULL)""")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except sqlite3.Error:
            if db and db.in_transaction:
                db.execute("ROLLBACK")
            raise GatewayError("dry_ledger_unavailable") from None
        except Exception:
            if db and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            if db:
                db.close()

    def acknowledge(self, request, now, validator):
        self.contracts.validate("order-request", request)
        if len(canonical(request).encode()) > 16_384 or len(request["client_order_id"]) > 256:
            raise GatewayError("command_too_large")
        key, encoded = request["client_order_id"], canonical(request)
        with self.transaction() as db:
            row = db.execute(
                "SELECT request, acknowledgement, state FROM commands WHERE command_id=?", (key,)
            ).fetchone()
            if row:
                if row[0] != encoded:
                    raise GatewayError("command_id_collision")
                if row[1] is not None:
                    return loads(row[1]), True
                # An imported/interrupted uncertain outcome is never retried or inferred flat.
                code, state = "uncertain_outcome_reconciliation_required", "uncertain"
            else:
                try:
                    validator(request, db)
                    code, state = "dry_run_not_sent", "not_sent"
                except GatewayError as error:
                    code, state = str(error), "refused"
                db.execute(
                    "INSERT INTO commands VALUES (?, ?, NULL, ?, NULL)", (key, encoded, state)
                )
                db.execute(
                    "INSERT INTO journal VALUES (NULL, ?, 'intent', ?, ?)",
                    (key, stamp(now), "dry_intent"),
                )
            update = {
                "schema_version": 1,
                "client_order_id": key,
                "instrument": request["instrument"],
                "status": "accepted" if state == "not_sent" else "rejected",
                "ts": stamp(now),
                "reason": code,
            }
            for field in ("side", "size"):
                if field in request:
                    update[field] = request[field]
            acknowledgement = {
                "schema_version": 1,
                "mode": "dry",
                "acknowledgement_id": hashlib.sha256((key + encoded).encode()).hexdigest(),
                "dispatch_state": state,
                "update": update,
            }
            self.contracts.validate("gateway-acknowledgement", acknowledgement)
            db.execute(
                "UPDATE commands SET acknowledgement=? WHERE command_id=?",
                (canonical(acknowledgement), key),
            )
            db.execute(
                "INSERT INTO journal VALUES (NULL, ?, 'acknowledgement', ?, ?)",
                (key, stamp(now), code),
            )
        return acknowledgement, False

    def record_uncertain(self, request, now, *, broker_id=None):
        """Synthetic recovery input only; not an execution or resolution method."""
        self.contracts.validate("order-request", request)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO commands VALUES (?, ?, NULL, 'uncertain', ?)",
                (request["client_order_id"], canonical(request), broker_id),
            )
            db.execute(
                "INSERT INTO journal VALUES (NULL, ?, 'recovery', ?, ?)",
                (request["client_order_id"], stamp(now), "uncertain_outcome"),
            )

    def bind_recorded_identifier(self, key, broker_id):
        """Map explicit synthetic recorded evidence. Never used by the HTTP service."""
        if not isinstance(broker_id, str) or not 1 <= len(broker_id) <= 256:
            raise GatewayError("invalid_broker_identifier")
        with self.transaction() as db:
            row = db.execute("SELECT broker_id FROM commands WHERE command_id=?", (key,)).fetchone()
            if row is None or row[0] not in {None, broker_id}:
                raise GatewayError("broker_identifier_conflict")
            other = db.execute("SELECT command_id FROM commands WHERE broker_id=?", (broker_id,))
            if any(found[0] != key for found in other):
                raise GatewayError("broker_identifier_conflict")
            db.execute("UPDATE commands SET broker_id=? WHERE command_id=?", (broker_id, key))

    def counts(self):
        with self.transaction() as db:
            return {
                "commands": db.execute("SELECT COUNT(*) FROM commands").fetchone()[0],
                "acknowledgements": db.execute(
                    "SELECT COUNT(*) FROM journal WHERE kind='acknowledgement'"
                ).fetchone()[0],
                "uncertain": db.execute(
                    "SELECT COUNT(*) FROM commands WHERE state='uncertain'"
                ).fetchone()[0],
            }
