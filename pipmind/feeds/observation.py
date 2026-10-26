"""Bounded, cached observation of one configured trader; no runtime operations.

Only projected fields cross the SQL connection. Snapshot isolation must already
be enabled: this reader never takes the writer application lock or runs DDL.
"""

import os
import re
from copy import deepcopy
from threading import Lock
from time import monotonic

from pipmind.contracts import ContractError, loads, stamp, utc
from pipmind.feeds.sql_store import SqlFeedStore
from pipmind.feeds.store import encoded
from pipmind.shadow.sql import close_session

MAX_RESPONSE_BYTES = 262144
MAX_WORKING_BYTES = 65536
MAX_USAGE_BYTES = 8192
MAX_TEXT_BYTES = 16000
MAX_LIST = 100
RECENT_LIMIT = 20
CACHE_SECONDS = 30


class ObservationError(ValueError):
    """Fixed private API error code, never a driver message."""


def bounded_sql(field, maximum):
    # Callers supply only module-owned SQL expressions, never request input.
    return f"CASE WHEN DATALENGTH({field})<={maximum} THEN {field} END"


EVENT_AT_SQL = (
    "JSON_VALUE(CASE WHEN ISJSON(i.envelope)=1 THEN i.envelope ELSE '{}' END, '$.available_at')"
)
OUTCOME_KEYS_SQL = "SELECT TOP (21) pk,event_id FROM pm_outcomes WHERE trader=%s ORDER BY pk DESC"
OUTCOME_SQL = f"""
SELECT TOP (2) o.pk,o.event_id,
 {bounded_sql("p.recorded_at", 80)}, {bounded_sql("p.status", 128)},
 {bounded_sql("p.action", 64)}, {bounded_sql("p.instrument", 256)},
 {bounded_sql("p.reasoning", MAX_TEXT_BYTES)},
 CASE WHEN DATALENGTH(p.reasoning)>{MAX_TEXT_BYTES} THEN 1 ELSE 0 END,
 {bounded_sql("p.usage", MAX_USAGE_BYTES)},
 CASE WHEN DATALENGTH(p.usage)>{MAX_USAGE_BYTES} THEN 1 ELSE 0 END,
 {bounded_sql("p.error", 256)},
 {bounded_sql(EVENT_AT_SQL, 80)},
 CASE WHEN DATALENGTH(p.recorded_at)>80 OR DATALENGTH(p.status)>128
 OR DATALENGTH(p.action)>64 OR DATALENGTH(p.instrument)>256
 OR DATALENGTH(p.error)>256 OR DATALENGTH({EVENT_AT_SQL})>80 THEN 1 ELSE 0 END
FROM pm_outcomes o
LEFT JOIN pm_inputs i ON i.trader=o.trader AND i.id=o.event_id
OUTER APPLY OPENJSON(CASE WHEN ISJSON(o.result)=1 THEN o.result ELSE '{{}}' END)
WITH (
 recorded_at nvarchar(max) '$.ts', status nvarchar(max) '$.status',
 action nvarchar(max) '$.decision.action', instrument nvarchar(max) '$.decision.instrument',
 reasoning nvarchar(max) '$.decision.reasoning', usage nvarchar(max) '$.usage' AS JSON,
 error nvarchar(max) '$.error'
) p
WHERE o.trader=%s AND o.pk=%s
ORDER BY o.pk DESC
"""


def recent_outcomes(cur, trader):
    # Materialize only narrow keys before touching audit JSON. TOP in a single
    # LOB-carrying query does not guarantee bounded candidate-plan LOB work.
    cur.execute(OUTCOME_KEYS_SQL, (trader,))
    keys = cur.fetchall()
    if (
        len(keys) > RECENT_LIMIT + 1
        or any(
            len(row) != 2
            or type(row[0]) is not int
            or row[0] <= 0
            or not isinstance(row[1], str)
            or not 1 <= len(row[1]) <= 256
            for row in keys
        )
        or any(left[0] <= right[0] for left, right in zip(keys, keys[1:], strict=False))
        or len({row[1] for row in keys}) != len(keys)
    ):
        raise ObservationError("observation_window_invalid")
    outcomes = []
    for pk, identifier in keys[:RECENT_LIMIT]:
        # A bulk IN plan can still scan/parse historical LOBs. A measured point
        # predicate bounds each projection without changing the SQL capacity.
        cur.execute(OUTCOME_SQL, (trader, pk))
        rows = cur.fetchall()
        # Refuse missing/duplicate/wrong-trader rows instead of relabeling an
        # incomplete projection as the captured recent window. TOP2 exposes
        # malformed JSON producing duplicate rows without unbounded transfer.
        if len(rows) != 1 or tuple(rows[0][:2]) != (pk, identifier):
            raise ObservationError("observation_window_invalid")
        outcomes.append(outcome_projection(rows[0][1:]))
    return outcomes, len(keys) > RECENT_LIMIT


def timestamp(value):
    try:
        return stamp(utc(value))
    except ContractError:
        return None


def safe_code(value):
    # Error bodies and exception messages may contain private connection details.
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,128}", value) else None


def working_projection(raw):
    issues = []
    try:
        value = loads(raw) if raw is not None else None
    except (ContractError, TypeError, RecursionError):
        value = None
    if not isinstance(value, dict):
        value = {}
        issues.append("working_missing_invalid_or_oversize")
    result = {}
    for key in ("views", "waiting_for", "position_theses"):
        rows = value.get(key)
        valid = isinstance(rows, list) and len(rows) <= MAX_LIST
        if valid:
            if key == "position_theses":
                valid = all(
                    isinstance(row, dict)
                    and all(isinstance(row.get(k), str) for k in ("instrument", "thesis", "as_of"))
                    and timestamp(row["as_of"]) is not None
                    for row in rows
                )
                rows = (
                    [{k: row[k] for k in ("instrument", "thesis", "as_of")} for row in rows]
                    if valid
                    else None
                )
            else:
                valid = all(isinstance(row, str) for row in rows)
        if not valid:
            issues.append("working_" + key + "_missing_invalid_or_oversize")
        result[key] = rows if valid else None
    return result, issues


def token_projection(raw, oversize):
    result = {"input_tokens": None, "output_tokens": None, "complete": False}
    try:
        rows = loads(raw) if raw is not None and not oversize else None
    except (ContractError, TypeError, RecursionError):
        rows = None
    if not isinstance(rows, list) or not rows or len(rows) > 16:
        return result
    complete = True
    for key in ("input_tokens", "output_tokens"):
        values = [row.get(key) if isinstance(row, dict) else None for row in rows]
        known = all(type(value) is int and 0 <= value <= 1_000_000_000 for value in values)
        result[key] = sum(values) if known else None
        complete = complete and known
    result["complete"] = complete
    return result


def outcome_projection(row):
    (
        identifier,
        recorded,
        status,
        action,
        instrument,
        reasoning,
        big_reason,
        usage,
        big_usage,
        error,
        at,
        big_scalar,
    ) = row
    issues = []
    if big_scalar:
        issues.append("scalar_fields_oversize")
    if not timestamp(recorded):
        issues.append("recorded_at_missing_or_invalid")
    if not timestamp(at):
        issues.append("event_at_missing_or_invalid")
    if not safe_code(status):
        issues.append("status_missing_or_invalid")
    if reasoning is None:
        issues.append("reasoning_oversize" if big_reason else "reasoning_not_recorded")
    tokens = token_projection(usage, big_usage)
    if not tokens["complete"]:
        issues.append("tokens_oversize" if big_usage else "tokens_incomplete_or_not_recorded")
    return {
        "event_id": identifier,
        "event_at": timestamp(at),
        "recorded_at": timestamp(recorded),
        "status": safe_code(status),
        "action": action if action in {"enter", "add", "reduce", "exit", "hold"} else None,
        "instrument": instrument,
        "reasoning": reasoning,
        "tokens": tokens,
        "errors": [
            error
            if error
            in {
                "model_unavailable_or_invalid",
                "unavailable_shadow_evidence",
                "future_shadow_memory",
            }
            else "error_details_withheld"
        ]
        if error
        else [],
        "issues": issues,
    }


def read_snapshot(connect, trader, moment):
    from pytds.extensions import ISOLATION_LEVEL_SNAPSHOT

    db = None
    try:
        # Capability metadata is read outside the snapshot. A database with
        # snapshot disabled can reject even the first query in that mode.
        db = connect()
        cur = db.cursor()
        cur.execute("SELECT snapshot_isolation_state FROM sys.databases WHERE database_id=DB_ID()")
        if cur.fetchone()[0] != 1:
            raise ObservationError("observation_snapshot_required")
        close_session(db)
        db = None
        db = connect(isolation_level=ISOLATION_LEVEL_SNAPSHOT)
        cur = db.cursor()
        cur.execute("SET NOCOUNT ON; SET XACT_ABORT ON; SET LOCK_TIMEOUT 2000")
        cur.execute(
            "SELECT revision,paused,CASE WHEN DATALENGTH(working)<=65536 THEN working END "
            "FROM pm_minds WHERE trader=%s",
            (trader,),
        )
        head = cur.fetchone()
        if not head:
            raise ObservationError("observation_trader_missing")
        working, issues = working_projection(head[2])
        outcomes, has_more = recent_outcomes(cur, trader)
        if any(row["issues"] for row in outcomes):
            issues.append("recent_outcomes_partial")
        result = {
            "schema_version": "pipmind-observation-v1",
            "observed_at": stamp(moment),
            "status": "partial" if issues else "ok",
            "source": {"kind": "sql_snapshot", "consistency": "snapshot"},
            "head": {"revision": head[0], "paused": bool(head[1]), "working": working},
            "recent_outcomes": outcomes,
            "recent_window": {
                "limit": RECENT_LIMIT,
                "has_more": has_more,
                "order": "newest_commit_first",
            },
            "issues": issues,
        }
        if len(encoded(result).encode()) > MAX_RESPONSE_BYTES:
            raise ObservationError("observation_response_bound")
        # No commit or rollback handshake is necessary: closing this non-pooled
        # read-only session ends its snapshot without a potentially blocking RPC.
        return result
    except ObservationError:
        raise
    except Exception:
        raise ObservationError("observation_unavailable") from None
    finally:
        close_session(db)


class ObservationReader:
    def __init__(self, store, *, enabled=False, trader="", clock=monotonic):
        self.store, self.enabled, self.trader, self.clock = store, enabled, trader, clock
        self.lock = Lock()
        self.attempted_at = None
        self.observed_clock = None
        self.cached = None
        self.error = None

    @classmethod
    def environment(cls, store):
        return cls(
            store,
            enabled=os.environ.get("PIPMIND_OBSERVATION_READS") == "true",
            trader=os.environ.get("PIPMIND_SHADOW_TRADER", ""),
        )

    def get(self, moment):
        if not self.enabled:
            raise ObservationError("observation_disabled")
        if not isinstance(self.store, SqlFeedStore):
            raise ObservationError("observation_sql_required")
        if (
            not isinstance(self.trader, str)
            or not 1 <= len(self.trader) <= 128
            or not self.trader.isascii()
        ):
            raise ObservationError("observation_trader_required")
        with self.lock:
            instant = self.clock()
            if self.attempted_at is None or instant - self.attempted_at >= CACHE_SECONDS:
                try:
                    self.cached = read_snapshot(self.store.connect, self.trader, moment)
                    self.observed_clock = instant
                    self.error = None
                except ObservationError as error:
                    self.error = str(error)
                self.attempted_at = self.clock()
            if self.cached is None:
                raise ObservationError(self.error)
            result = deepcopy(self.cached)
            if self.error:
                result["status"] = "partial"
                result["issues"].append("observation_refresh_unavailable")
            result["cache"] = {
                "minimum_refresh_seconds": CACHE_SECONDS,
                "age_seconds": max(0, int(instant - self.observed_clock)),
                "stale": self.error is not None,
            }
            return result
