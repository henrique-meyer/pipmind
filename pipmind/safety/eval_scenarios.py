"""Explicit synthetic SQL evaluation inputs, never owner defaults or enforcement."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pipmind.contracts import Contracts, stamp, utc
from pipmind.reconciliation import Binding, Freshness, SnapshotFacts
from pipmind.safety.admission import (
    AdmissionRequest,
    ReconciliationEvidence,
    checkpoint_digest,
    decode_request,
    encode_policy_book,
    encode_request,
    policy_book_digest,
)
from pipmind.safety.boundary import Conversion, ProposalFacts, RiskFacts, RiskPolicy
from pipmind.safety.session import (
    EquityObservation,
    SessionBinding,
    SessionSpec,
    advance,
    encode_observation,
    encode_spec,
    initial_state,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "evals/safety/sql-enforcement-v1.json"
SOURCE_PATH = ROOT / "evals/fixtures/starter-v1.jsonl"
PROFILE_VERSION = "sql-safety-v1"
CASE_IDS = ("S02", "S03", "S04", "S05", "S06", "S07")
_MECHANISMS = ("quote_age", "account_age", "oversize", "loss_latch", "controls", "stale_owner")


class ScenarioRefused(Exception):
    """Fixed invalid or changed synthetic profile/source input."""


def _need(ok):
    if not ok:
        raise ScenarioRefused("invalid_sql_eval_scenario")


def _json(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _sha(raw):
    _need(type(raw) in (str, bytes))
    return hashlib.sha256(raw if type(raw) is bytes else raw.encode("utf-8")).hexdigest()


def _common(value):
    keys = {
        "synthetic_only",
        "currency",
        "action_mapping",
        "positive_size",
        "order_type",
        "stop",
        "base_units_per_size",
        "opening_equity",
        "breach_equity",
        "breach_offset_seconds",
        "margin_used",
        "margin_available",
        "proposal_margin",
        "session_start_offset_seconds",
        "session_end_offset_seconds",
        "session_snapshot_max_age_seconds",
        "stale_account_seconds",
        "stale_quote_seconds",
        "mapping",
        "policy",
    }
    _need(type(value) is dict and set(value) == keys and value["synthetic_only"] is True)
    _need(value["currency"] == "USD" and value["order_type"] == "market")
    _need(value["action_mapping"] == {"enter": "open"})
    _need(type(value["mapping"]) is list and len(value["mapping"]) == 1)
    _need(
        set(value["mapping"][0])
        == {
            "instrument",
            "market_id",
            "unit",
            "quantity_factor",
            "increment",
            "minimum",
            "stale_seconds",
        }
        and value["mapping"][0]["instrument"] == "EUR/USD"
        and value["mapping"][0]["stale_seconds"] == 15
    )
    policy = value["policy"]
    numeric = {
        "max_order_base_units",
        "max_instrument_base_units",
        "max_gross_exposure",
        "max_planned_order_loss",
        "max_total_planned_loss",
        "max_daily_loss",
        "max_daily_risk",
        "min_margin_available",
    }
    counts = {
        "max_session_orders",
        "snapshot_max_age_seconds",
        "quote_max_age_seconds",
        "conversion_max_age_seconds",
    }
    _need(type(policy) is dict and set(policy) == numeric | counts)
    for key in numeric:
        _need(
            type(policy[key]) is str
            and Decimal(policy[key]).is_finite()
            and Decimal(policy[key]) >= 0
        )
    for key in counts:
        _need(type(policy[key]) is int and 0 < policy[key] <= 86400)
    for key in (
        "positive_size",
        "stop",
        "base_units_per_size",
        "opening_equity",
        "breach_equity",
        "margin_used",
        "margin_available",
        "proposal_margin",
    ):
        _need(
            type(value[key]) is str and Decimal(value[key]).is_finite() and Decimal(value[key]) > 0
        )
    for key in (
        "breach_offset_seconds",
        "session_start_offset_seconds",
        "session_end_offset_seconds",
        "session_snapshot_max_age_seconds",
        "stale_account_seconds",
        "stale_quote_seconds",
    ):
        _need(type(value[key]) is int and abs(value[key]) <= 86400)
    _need(
        value["session_start_offset_seconds"] < 0 < value["session_end_offset_seconds"]
        and 0 < value["breach_offset_seconds"] < -value["session_start_offset_seconds"]
        and 0 < value["stale_quote_seconds"] > policy["quote_max_age_seconds"]
        and policy["snapshot_max_age_seconds"]
        < value["stale_account_seconds"]
        < value["session_snapshot_max_age_seconds"]
    )


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    case_id: str
    title: str
    split: str
    source_case_sha256: str
    supplement_sha256: str
    source_case_body: str
    supplement_body: str


@dataclass(frozen=True, slots=True)
class PreparedScenario:
    case_id: str
    variant: str
    source_case_sha256: str
    supplement_sha256: str
    metadata_body: str
    policy_book_body: str
    session_spec_body: str
    observation_bodies: tuple[str, ...]
    request_body: str
    mapping_body: str
    control_action: str | None
    replace_lease: bool


def load_scenarios():
    """Read only the fixed local profile and exact original starter bytes."""
    try:
        source = SOURCE_PATH.read_bytes()
        _need(len(source) <= 1048576 and PROFILE_PATH.stat().st_size <= 65536)
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        _need(
            type(profile) is dict
            and set(profile) == {"schema_version", "profile_version", "source", "common", "cases"}
            and type(profile["schema_version"]) is int
            and profile["schema_version"] == 1
            and profile["profile_version"] == PROFILE_VERSION
            and profile["source"]
            == {
                "path": "evals/fixtures/starter-v1.jsonl",
                "sha256": _sha(source),
                "case_hash_domain": "raw-utf8-jsonl-line-without-lf",
                "clock_anchor": "events[0].available_at",
            }
        )
        rows = {}
        for line in source.split(b"\n"):
            if not line:
                continue
            value = json.loads(line)
            _need(value["case_id"] not in rows)
            rows[value["case_id"]] = line.decode("utf-8")
        _need(tuple(row["case_id"] for row in profile["cases"]) == CASE_IDS)
        _common(profile["common"])
        result = []
        for index, supplement in enumerate(profile["cases"]):
            case_id = CASE_IDS[index]
            body = rows[case_id]
            case = json.loads(body)
            Contracts().validate("scenario", case)
            _need(
                set(supplement) == {"case_id", "source_case_sha256", "mechanism", "variants"}
                and supplement["source_case_sha256"] == _sha(body)
                and supplement["mechanism"] == _MECHANISMS[index]
                and supplement["variants"]
                == (["positive", "pause", "kill"] if case_id == "S06" else ["positive", "target"])
                and case["split"] == "development"
                and len(case["events"]) == 1
            )
            supplement_body = _json(
                {
                    "profile_version": PROFILE_VERSION,
                    "common": profile["common"],
                    "case": supplement,
                }
            )
            result.append(
                ScenarioSpec(
                    case_id,
                    case["title"],
                    case["split"],
                    _sha(body),
                    _sha(supplement_body),
                    body,
                    supplement_body,
                )
            )
        return tuple(result)
    except Exception:
        raise ScenarioRefused("invalid_sql_eval_scenario") from None


def _inputs(spec, variant):
    _need(type(spec) is ScenarioSpec and spec.case_id in CASE_IDS)
    _need(
        all(
            type(value) is str
            for value in (
                spec.case_id,
                spec.title,
                spec.split,
                spec.source_case_sha256,
                spec.supplement_sha256,
                spec.source_case_body,
                spec.supplement_body,
            )
        )
    )
    _need(_sha(spec.source_case_body) == spec.source_case_sha256)
    _need(_sha(spec.supplement_body) == spec.supplement_sha256)
    source = json.loads(spec.source_case_body, parse_float=Decimal)
    supplement = json.loads(spec.supplement_body)
    _common(supplement["common"])
    _need(
        source["case_id"] == spec.case_id
        and source["split"] == spec.split == "development"
        and supplement["profile_version"] == PROFILE_VERSION
        and supplement["case"]["case_id"] == spec.case_id
        and supplement["case"]["source_case_sha256"] == spec.source_case_sha256
        and type(variant) is str
        and variant in supplement["case"]["variants"]
    )
    return source, supplement["common"]


def prepare_scenario(spec, *, sql_now, account_id, session_id, variant):
    """Freeze complete synthetic inputs; actual SQL stores enforce their meaning."""
    try:
        return _prepare(spec, sql_now, account_id, session_id, variant)
    except ScenarioRefused:
        raise
    except Exception:
        raise ScenarioRefused("invalid_sql_eval_scenario") from None


def _prepare(spec, sql_now, account_id, session_id, variant):
    _need(type(sql_now) is datetime and sql_now.tzinfo is UTC)
    source, common = _inputs(spec, variant)
    event = source["events"][0]
    reply = source["model_responses"][event["message_id"]]["reply"]
    _need(reply["action"] == "enter" and common["action_mapping"] == {"enter": "open"})
    target = variant != "positive"
    account_offset = -common["stale_account_seconds"] if spec.case_id == "S03" and target else 0
    quote_offset = (
        -common["stale_quote_seconds"] if spec.case_id == "S02" and target else account_offset
    )
    when, account_at, quote_at = (
        stamp(sql_now),
        stamp(sql_now + timedelta(seconds=account_offset)),
        stamp(sql_now + timedelta(seconds=quote_offset)),
    )
    start = stamp(sql_now + timedelta(seconds=common["session_start_offset_seconds"]))
    end = stamp(sql_now + timedelta(seconds=common["session_end_offset_seconds"]))
    binding = SessionBinding(account_id, "demo", common["currency"])
    values = common["policy"]
    policy = RiskPolicy(
        "sql-eval-policy-v1",
        account_id,
        "demo",
        common["currency"],
        reply["instrument"],
        *(
            Decimal(values[key])
            for key in (
                "max_order_base_units",
                "max_instrument_base_units",
                "max_gross_exposure",
                "max_planned_order_loss",
                "max_total_planned_loss",
                "max_daily_loss",
                "max_daily_risk",
                "min_margin_available",
            )
        ),
        values["max_session_orders"],
        values["snapshot_max_age_seconds"],
        values["quote_max_age_seconds"],
        values["conversion_max_age_seconds"],
    )
    book = (policy,)
    session = SessionSpec(
        binding,
        session_id,
        policy.policy_id,
        policy_book_digest(book),
        start,
        end,
        Decimal(common["opening_equity"]),
        "synthetic-opening-evidence",
        policy.max_daily_loss,
        common["session_snapshot_max_age_seconds"],
    )
    payload = event["payload"]
    account = {
        "schema_version": 1,
        "as_of": account_at,
        "currency": binding.currency,
        "balance": Decimal(common["opening_equity"]),
        "equity": Decimal(common["opening_equity"]),
        "margin_used": Decimal(common["margin_used"]),
        "margin_available": Decimal(common["margin_available"]),
    }
    observed = SnapshotFacts(
        Binding(account_id, "demo"),
        account,
        {"schema_version": 1, "as_of": account_at, "positions": []},
        (),
        True,
        True,
        (),
        account_at,
        True,
        (),
        account_at,
        True,
    )
    evidence = ReconciliationEvidence(
        deepcopy(observed),
        observed,
        Freshness(timedelta(seconds=values["snapshot_max_age_seconds"]), timedelta(seconds=2)),
        (),
        (),
        (),
        (),
        (),
    )
    conversion = Conversion("USD", "USD", Decimal("1"), account_at)
    quote = ProposalFacts(
        reply["instrument"],
        Decimal(str(payload["bid"])),
        Decimal(str(payload["ask"])),
        quote_at,
        Decimal(common["base_units_per_size"]),
        Decimal(common["proposal_margin"]),
        conversion,
    )
    facts = RiskFacts(
        account_id,
        "demo",
        account_at,
        True,
        True,
        False,
        False,
        start,
        end,
        Decimal(common["opening_equity"]),
        Decimal("0"),
        0,
        quote,
        (),
    )
    checksum = checkpoint_digest(evidence, facts)
    observations = []
    if spec.case_id == "S05" and target:
        breach_at = stamp(sql_now - timedelta(seconds=common["breach_offset_seconds"]))
        observations.append(
            EquityObservation(
                binding,
                session_id,
                "synthetic-breach",
                breach_at,
                breach_at,
                Decimal(common["breach_equity"]),
                Decimal("0"),
                True,
                True,
                start,
                breach_at,
                _sha("synthetic-breach-equity:" + common["breach_equity"]),
                "synthetic-cash-flow-zero",
            )
        )
    observations.append(
        EquityObservation(
            binding,
            session_id,
            "synthetic-current",
            account_at,
            when,
            Decimal(common["opening_equity"]),
            Decimal("0"),
            True,
            True,
            start,
            account_at,
            checksum,
            "synthetic-cash-flow-zero",
        )
    )
    state = initial_state(session)
    for observation in observations:
        state, _ = advance(state, observation, now=sql_now)
    order = {
        "schema_version": 1,
        "client_order_id": f"sql-eval-{spec.case_id.lower()}-{variant}",
        "action": "open",
        "instrument": reply["instrument"],
        "side": reply["side"],
        "size": (
            Decimal(common["positive_size"])
            if spec.case_id == "S04" and not target
            else Decimal(str(reply["size"]))
        ),
        "order_type": common["order_type"],
        "stop": Decimal(common["stop"]),
    }
    request = AdmissionRequest(order, facts, evidence, (), state.revision, state.latest_sha256)
    request_body = encode_request(request)
    # Validate the captured public codec before it is exposed to another component.
    _need(encode_request(decode_request(request_body)) == request_body)
    delta = sql_now - utc(event["available_at"])
    offset = ((delta.days * 86400 + delta.seconds) * 1000000) + delta.microseconds
    metadata = {
        "profile_version": PROFILE_VERSION,
        "case_id": spec.case_id,
        "variant": variant,
        "source_clock_start": source["clock_start"],
        "source_available_at": event["available_at"],
        "sql_observed_at": when,
        "offset_microseconds": offset,
        "clock_anchor": "source_available_at",
        "account_offset_seconds": account_offset,
        "quote_offset_seconds": quote_offset,
        "source_account_as_of": source["initial_account"]["as_of"],
        "source_quote_as_of": payload["venue_ts"],
        "source_quote_stale": payload["stale"],
        "supplemented_account_scope": "complete-empty-book-with-zero-external-cash-flow",
    }
    return PreparedScenario(
        spec.case_id,
        variant,
        spec.source_case_sha256,
        spec.supplement_sha256,
        _json(metadata),
        encode_policy_book(book),
        encode_spec(session),
        tuple(encode_observation(row) for row in observations),
        request_body,
        _json(common["mapping"]),
        variant if spec.case_id == "S06" and target else None,
        spec.case_id == "S07" and target,
    )


def captured_order(request_body):
    """Project the existing public admission codec; no new numeric representation."""
    try:
        packed = json.loads(encode_request(decode_request(request_body)))
        _need(packed[:2] == ["record", "AdmissionRequest"])
        return _json(next(value for name, value in packed[2] if name == "order"))
    except Exception:
        raise ScenarioRefused("invalid_sql_eval_scenario") from None


def expected_order(spec, variant):
    prepared = prepare_scenario(
        spec,
        sql_now=datetime(2026, 10, 22, tzinfo=UTC),
        account_id="sql-eval-reference",
        session_id="sql-eval-reference-session",
        variant=variant,
    )
    return captured_order(prepared.request_body)
