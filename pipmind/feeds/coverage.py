"""Private, bounded observations of saved feed checkpoints; never fetches sources."""

from datetime import UTC, datetime
from math import isfinite

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.social import read_observation
from pipmind.feeds.store import (
    COVERAGE_ACCOUNTS,
    COVERAGE_STATE_BYTES,
    COVERAGE_TOTAL_BYTES,
    encoded,
)

MAX_PAGE = 100
MAX_RESPONSE_BYTES = 262144
PRODUCERS = ("macro", "news", "social")


def mapping(value):
    return value if isinstance(value, dict) else {}


def instant(value):
    try:
        if isinstance(value, str) and len(value) <= 40:
            return utc(value)
        if type(value) in (int, float) and isfinite(value):
            return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def timestamp(value):
    parsed = instant(value)
    return stamp(parsed) if parsed is not None else None


def read_state(success, error, *, unknown=False):
    left, right = instant(success), instant(error)
    if unknown or (success is not None and left is None) or (error is not None and right is None):
        status = "unknown"
    elif left is None and right is None:
        status = "unseen"
    elif left is not None and right is not None and left == right:
        status = "ambiguous"
    elif right is not None and (left is None or right > left):
        status = "failed"
    else:
        status = "successful_read"
    return {
        "state": status,
        "last_success_at": timestamp(success),
        "last_error_at": timestamp(error),
    }


def source_keys(channel, recipe):
    if channel == "macro":
        return [
            "canonical:quality",
            "canonical:bootstrap:" + identity(recipe["currency"], recipe["indicator"]),
        ]
    sid = recipe["id"]
    if channel == "news":
        return ["rss:" + kind + ":" + sid for kind in ("last", "error", "poll", "http")]
    return ["role:" + sid, "role:check:" + sid, "primary:status:" + sid, "primary:poll:" + sid]


def macro_row(recipe, state):
    series = recipe["currency"] + ":" + recipe["indicator"]
    quality = mapping(mapping(state.get("canonical:quality")).get(series))
    bootstrap = mapping(
        state.get("canonical:bootstrap:" + identity(recipe["currency"], recipe["indicator"]))
    )
    status = quality.get("status")
    if type(status) is not str or status not in {"partial", "retained_window_captured"}:
        status = "unknown"
    bootstrap_status = bootstrap.get("status")
    if type(bootstrap_status) is not str or bootstrap_status not in {
        "latest_retained_row_only",
        "latest_unavailable",
    }:
        bootstrap_status = "unknown"
    successes = [
        instant(quality.get("checked_at")) if status != "unknown" else None,
        instant(bootstrap.get("captured_at")) if bootstrap_status != "unknown" else None,
    ]
    latest = max((value for value in successes if value is not None), default=None)
    return {
        "source_id": series if len(series) <= 104 else "series-sha256:" + identity(series),
        "currency": recipe["currency"],
        "indicator": recipe["indicator"] if len(recipe["indicator"]) <= 100 else None,
        "read": read_state(stamp(latest) if latest else None, None, unknown=latest is None),
        "success_scope": "canonical_page_only",
        "live_delivery_status": "unknown",
        "backfill": {"window_status": status, "latest_bootstrap_status": bootstrap_status},
        "retry_not_before_at": None,
    }


def news_row(recipe, state):
    sid = recipe["id"]
    http = mapping(state.get("rss:http:" + sid))
    failures = http.get("failures")
    error_key = "rss:error:" + sid
    error = mapping(state.get(error_key)).get("checked_at")
    uncertain = (error_key in state and error is None) or (
        type(failures) is int and failures > 0 and error is None
    )
    return {
        "source_id": sid,
        "read": read_state(state.get("rss:last:" + sid), error, unknown=uncertain),
        "success_scope": "rss_conditional_read_or_committed_page",
        "consecutive_failures": failures if type(failures) is int and 0 <= failures <= 16 else None,
        "validators_retained": {
            "etag": bool(http.get("etag")),
            "last_modified": bool(http.get("last_modified")),
        },
        "poll_not_before_at": timestamp(state.get("rss:poll:" + sid)),
        "retry_not_before_at": timestamp(state.get("rss:poll:" + sid))
        if type(failures) is int and failures > 0
        else None,
        "backfill": {"window_status": "unknown"},
    }


def account_read(account, state, unavailable, now):
    user_id = account.get("user_id")
    valid_id = (
        isinstance(user_id, str)
        and user_id.isascii()
        and user_id.isdigit()
        and 1 <= len(user_id) <= 19
    )
    key = "x:read:" + user_id if valid_id else None
    raw = state.get(key) if key else None
    observation = None
    if raw is not None and key not in unavailable:
        try:
            observation = read_observation(raw, user_id)
        except FeedError:
            pass
    known = observation is not None
    read = read_state(
        observation["last_success_at"] if known else None,
        observation["last_error_at"] if known else None,
        unknown=not known,
    )
    anomaly = any(
        now is not None and instant(read[field]) is not None and instant(read[field]) > now
        for field in ("last_success_at", "last_error_at")
    )
    if anomaly:
        read["state"] = "unknown"
    return {
        "user_id": user_id if valid_id else None,
        "read": read,
        "latest_error_code": observation["last_error_code"] if known else None,
        "last_page": observation["page"] if known else None,
        "complete_history_proven": False,
        "timestamp_anomaly": anomaly,
        "state_availability": "bounded_unavailable" if key in unavailable else "available",
    }


def social_row(recipe, state, unavailable=(), now=None):
    sid = recipe["id"]
    role, primary = mapping(state.get("role:" + sid)), mapping(state.get("primary:status:" + sid))
    accounts = role.get("accounts")
    known_accounts = (
        accounts
        if isinstance(accounts, list) and all(isinstance(a, dict) for a in accounts)
        else None
    )
    return {
        "source_id": sid,
        "read": read_state(None, None, unknown=True),
        "success_scope": "per_account_page_reads_only_not_role_health",
        "account_reads": [
            account_read(account, state, unavailable, now)
            for account in (known_accounts or [])[:COVERAGE_ACCOUNTS]
        ],
        "account_read_bounds": {
            "total_retained_accounts": len(known_accounts) if known_accounts is not None else None,
            "maximum_returned": COVERAGE_ACCOUNTS,
            "all_accounts_returned": len(known_accounts) <= COVERAGE_ACCOUNTS
            if known_accounts is not None
            else False,
        },
        "identity": {
            "checked_at": timestamp(role.get("checked_at")),
            "configured_accounts": len(recipe["accounts"]),
            "retained_accounts": len(known_accounts) if known_accounts is not None else None,
            "unverified_accounts": sum(a.get("unverified") is True for a in known_accounts)
            if known_accounts is not None
            else None,
            "dormant_accounts": sum(a.get("dormant") is True for a in known_accounts)
            if known_accounts is not None
            else None,
            "next_check_not_before_at": timestamp(state.get("role:check:" + sid)),
        },
        "primary_mirror": {
            "configured": bool(recipe.get("primary_feed_url")),
            "failed": primary.get("failed") if type(primary.get("failed")) is bool else None,
            "last_published_at": timestamp(primary.get("latest")),
            "checked_at": None,
            "poll_not_before_at": timestamp(state.get("primary:poll:" + sid)),
        },
        "retry_not_before_at": timestamp(state.get("x:backoff")),
        "retry_scope": "shared_x_transport",
        "backfill": {"window_status": "unknown"},
    }


def project(store, configuration, now, *, offset=0, limit=100):
    if configuration is None:
        raise FeedError("feed_coverage_configuration_unavailable")
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= MAX_PAGE
    ):
        raise FeedError("invalid_coverage_page")
    configured = [
        (channel, index, recipe)
        for channel, field in (("macro", "macro"), ("news", "news"), ("social", "roles"))
        for index, recipe in enumerate(configuration[field])
    ]
    selected = configured[offset : offset + limit]
    keys = sorted(
        {
            "x:backoff",
            *(key for channel, _, recipe in selected for key in source_keys(channel, recipe)),
        }
    )
    snapshot = store.coverage_snapshot(keys)
    state, unavailable = snapshot["states"], set(snapshot["unavailable_keys"])
    rows = []
    for channel, index, recipe in selected:
        facts = (
            social_row(recipe, state, unavailable, now)
            if channel == "social"
            else {
                "macro": macro_row,
                "news": news_row,
            }[channel](recipe, state)
        )
        row = {"channel": channel, "recipe_index": index, **facts}
        missing = bool(unavailable.intersection(source_keys(channel, recipe)))
        if channel == "social":
            missing = (
                missing
                or (
                    row["account_read_bounds"]["total_retained_accounts"] is not None
                    and not row["account_read_bounds"]["all_accounts_returned"]
                )
                or any(
                    a["state_availability"] == "bounded_unavailable" for a in row["account_reads"]
                )
            )
        anomaly = any(
            instant(row["read"][key]) is not None and instant(row["read"][key]) > now
            for key in ("last_success_at", "last_error_at")
        )
        if missing or anomaly:
            row["read"]["state"] = "unknown"
        row.update(
            {
                "state_availability": "bounded_unavailable" if missing else "available",
                "publication_time_precision": "unknown",
                "history_completeness": "unknown",
                "quarantined_count": None,
                "quarantine_attribution": "unknown",
                "timestamp_anomaly": anomaly,
            }
        )
        rows.append(row)
    alerts = {producer: None for producer in PRODUCERS}
    for producer, value in snapshot["alerts"]:
        when = instant(value)
        if (
            producer in alerts
            and when is not None
            and (alerts[producer] is None or when > alerts[producer])
        ):
            alerts[producer] = when
    result = {
        "schema_version": 1,
        "observed_at": stamp(now),
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total_configured": len(configured),
            "returned": len(rows),
            "next_offset": offset + len(rows) if offset + len(rows) < len(configured) else None,
        },
        "sources": rows,
        "shared": {
            "quarantined_by_producer": {
                p: snapshot["quarantined_by_producer"].get(p, 0) for p in PRODUCERS
            },
            "latest_retained_alert_at_by_producer": {
                p: stamp(value) if value else None for p, value in alerts.items()
            },
            "alert_scope": "latest_1000_retained_alerts_not_attributed_to_recipes",
            "x_retry_not_before_at": timestamp(state.get("x:backoff")),
        },
        "limits": {
            "sources_per_page": MAX_PAGE,
            "state_value_bytes": COVERAGE_STATE_BYTES,
            "state_total_bytes": COVERAGE_TOTAL_BYTES,
            "response_bytes": MAX_RESPONSE_BYTES,
        },
        "worker_liveness_used_as_source_success": False,
    }
    if len(encoded(result).encode()) > MAX_RESPONSE_BYTES:
        raise FeedError("feed_coverage_response_bound")
    return result
