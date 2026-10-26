"""Bounded, read-only CIAPI adapter. The sole POST is LogOn v2."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pipmind.contracts import ContractError, loads, stamp, utc
from pipmind.gateway.config import GatewayError, number

MAX_BODY = 2_000_000


def fields(data):
    """Accept documented PascalCase and current lower-camel DTO fields.

    Refuse ambiguous aliases rather than choosing an authentication/account value.
    Nested DTOs are normalized explicitly by their consuming operation.
    """
    if not isinstance(data, dict):
        raise GatewayError("invalid_broker_response")
    result = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise GatewayError("invalid_broker_response")
        name = key[0].upper() + key[1:]
        if name in result:
            raise GatewayError("ambiguous_broker_fields")
        result[name] = value
    return result


def broker_time(value):
    # Lightstreamer can retain one or two layers of escaped slash wrappers.
    # Match only that known encoding; never apply a general escape decoder.
    match = re.fullmatch(r"\\{0,2}/Date\((-?\d+)(?:[+-]0000)?\)\\{0,2}/", str(value))
    try:
        if match:
            return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(match[1]))
        return utc(value)
    except (OverflowError, ValueError):
        raise GatewayError("invalid_broker_timestamp") from None


@dataclass
class Response:
    status: int
    body: object
    retry_after: float = 0


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpTransport:
    def __init__(self):
        self.opener = build_opener(NoRedirect)

    def request(self, method, url, *, query=None, body=None, headers=None):
        url += ("?" + urlencode(query)) if query else ""
        request = Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **(headers or {}),
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise GatewayError("broker_response_too_large")
                return Response(response.status, loads(data))
        except HTTPError as error:
            # Error bodies/URLs may contain credentials or account values.
            try:
                retry = min(60, max(0, float(error.headers.get("Retry-After", 0))))
            except (ValueError, TypeError):
                retry = 0
            error.close()
            return Response(error.code, None, retry)
        except GatewayError:
            raise
        except ContractError:
            raise GatewayError("invalid_broker_response") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise GatewayError("broker_transport_unavailable") from None


class Ciapi:
    def __init__(self, config, symbols, *, transport=None, sleep=time.sleep, now=None):
        self.config, self.symbols = config, symbols
        self.transport, self.sleep = transport or HttpTransport(), sleep
        self.now = now or (lambda: datetime.now(UTC))
        self._session, self.generation = None, 0

    def login(self):
        cfg = self.config
        response = self.transport.request(
            "POST",
            cfg.api_root.rstrip("/") + "/v2/Session",
            body={
                "UserName": cfg.username,
                "Password": cfg.password,
                "AppKey": cfg.app_key,
                "AppVersion": "1",
                "AppComments": "PipMind dry reads",
            },
        )
        if response.status != 200 or not isinstance(response.body, dict):
            raise GatewayError("broker_login_failed")
        data = fields(response.body)
        if (
            not isinstance(data.get("Session"), str)
            or not 36 <= len(data["Session"]) <= 100
            or data.get("StatusCode", 0) != 0
            or type(data.get("StatusCode", 0)) is not int
            or data.get("PasswordChangeRequired", False) is not False
            or data.get("Is2FAEnabled", False) is not False
            or (data.get("AllowedAccountOperator") is False and cfg.environment != "demo")
            or type(data.get("AllowedAccountOperator", True)) is not bool
        ):
            raise GatewayError("broker_login_requires_attention")
        self._session = data["Session"]
        self.generation += 1

    def session(self):
        if self._session is None:
            self.login()
        return self._session

    def headlines(self):
        """Bounded Reuters FX headlines; never fetch article bodies."""
        data = fields(
            self.read(
                "/news/newsheadlines",
                query={"region": "FX", "cultureId": 69, "maxResults": 100},
                legacy=True,
            )
        )
        rows = data.get("Headlines")
        if not isinstance(rows, list) or len(rows) > 100:
            raise GatewayError("invalid_broker_headlines")
        observed = self.now()
        result = []
        for raw in rows:
            row = fields(raw)
            story, headline = row.get("StoryId"), row.get("Headline")
            if (
                not isinstance(story, str)
                or not 1 <= len(story) <= 512
                or not isinstance(headline, str)
                or not 1 <= len(headline) <= 2000
            ):
                raise GatewayError("invalid_broker_headlines")
            published = broker_time(row.get("PublishDate"))
            if published > observed:
                raise GatewayError("invalid_broker_headlines")
            identity = json.dumps([story, headline, stamp(published)], separators=(",", ":"))
            result.append(
                {
                    "schema_version": 1,
                    "relay_id": "reuters:" + hashlib.sha256(identity.encode()).hexdigest(),
                    "headline": headline,
                    "published_at": stamp(published),
                    "observed_at": stamp(observed),
                }
            )
        return result

    def read(self, path, *, query=None, legacy=False):
        # A fixed allowlist keeps even accidental future calls off the order path.
        if not (
            re.fullmatch(r"/v2/market/\d+/information", path)
            or path == "/v2/margin/clientAccountMargin"
            or path == "/v2/userAccount/ClientAndTradingAccount"
            or path
            in {
                "/order/openpositions",
                "/order/activestoplimitorders",
                "/order/tradehistory",
                "/order/stoplimitorderhistory",
                "/news/newsheadlines",
            }
            or re.fullmatch(r"/market/\d+/tickhistory", path)
            or re.fullmatch(r"/market/\d+/barhistorybefore", path)
        ):
            raise GatewayError("broker_endpoint_forbidden")
        root = self.config.legacy_root if legacy else self.config.api_root
        refreshed = False
        for attempt in range(3):
            try:
                response = self.transport.request(
                    "GET",
                    root.rstrip("/") + path,
                    query=query,
                    headers={
                        "UserName": self.config.username,
                        "Session": self.session(),
                    },
                )
            except GatewayError as error:
                if str(error) != "broker_transport_unavailable" or attempt == 2:
                    raise
                self.sleep(2**attempt)
                continue
            if response.status == 401 and not refreshed:
                self._session = None
                self.login()
                refreshed = True
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                if attempt < 2:
                    self.sleep(max(response.retry_after, 2**attempt))
                    continue
            if response.status != 200:
                raise GatewayError("broker_read_failed")
            if not isinstance(response.body, dict):
                raise GatewayError("invalid_broker_response")
            return response.body
        raise GatewayError("broker_retry_exhausted")

    def evidence(self, since):
        """Retain broker-origin reads independently of engine dispatch counters.

        The two legacy history endpoints use DIFFERENT documented units. A full
        result page is explicitly incomplete; it cannot prove no activity.
        """
        moment = self.now()
        if since.tzinfo is None or since > moment:
            raise GatewayError("invalid_broker_evidence_window")
        responses, complete, activity = {}, True, 0
        for path, key, scale in (
            ("openpositions", "OpenPositions", None),
            ("activestoplimitorders", "ActiveStopLimitOrders", None),
            ("tradehistory", "TradeHistory", 1),
            ("stoplimitorderhistory", "StopLimitOrderHistory", 1000),
        ):
            query = {"TradingAccountId": self.config.trading_account_id}
            if scale:
                query.update(maxResults=200)
                query["from"] = int(since.timestamp()) * scale
            raw = self.read("/order/" + path, query=query, legacy=True)
            body = fields(raw)
            rows = body.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise GatewayError("incomplete_broker_evidence")
            for row in rows:
                normalized = fields(row)
                if normalized.get("TradingAccountId") != self.config.trading_account_id:
                    raise GatewayError("broker_account_mismatch")
            # Supplemental open orders are additional activity, never ignored.
            extra = body.get("SupplementalOpenOrders", [])
            if not isinstance(extra, list):
                raise GatewayError("incomplete_broker_evidence")
            activity += len(rows) + len(extra)
            complete &= not scale or len(rows) < 200
            responses[path] = {"query": query, "body": raw}
        return {
            "as_of": stamp(moment),
            "from": stamp(since),
            "environment": self.config.environment,
            "binding_sha256": hashlib.sha256(
                json.dumps(
                    [
                        self.config.environment,
                        self.config.client_account_id,
                        self.config.trading_account_id,
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "responses": responses,
            "complete": bool(complete),
            "activity_count": activity,
        }

    def verify_symbols(self):
        for item in self.symbols.by_symbol.values():
            try:
                info = fields(
                    fields(
                        self.read(
                            f"/v2/market/{item.market_id}/information",
                            query={"clientAccountId": self.config.client_account_id},
                        )
                    )["MarketInformation"]
                )
                if info["MarketId"] != item.market_id or info["Name"] != item.instrument:
                    raise GatewayError("broker_symbol_mismatch")
                if number(info["IncrementSize"], positive=True) != item.increment:
                    raise GatewayError("broker_increment_mismatch")
                if number(info["WebMinSize"], positive=True) != item.minimum:
                    raise GatewayError("broker_minimum_mismatch")
            except (KeyError, TypeError):
                raise GatewayError("incomplete_broker_market") from None

    def verify_account(self):
        try:
            result = fields(self.read("/v2/userAccount/ClientAndTradingAccount"))
            result = fields(result.get("AccountResult", result))
            matches = [
                row
                for row in map(fields, result["TradingAccounts"])
                if row["TradingAccountId"] == self.config.trading_account_id
                and row["ClientAccountId"] == self.config.client_account_id
                and row["TradingAccountStatus"] == "Open"
            ]
            if len(matches) != 1:
                raise GatewayError("broker_account_mismatch")
            clients = [
                row
                for row in map(fields, result["ClientAccounts"])
                if row["ClientAccountId"] == self.config.client_account_id
            ]
            if len(clients) != 1:
                raise GatewayError("broker_account_mismatch")
            if self.config.environment == "demo":
                operators = [
                    row
                    for row in map(fields, result["AccountOperators"])
                    if row["AccountOperatorId"] == clients[0]["AccountOperatorId"]
                ]
                if (
                    len(operators) != 1
                    or number(operators[0].get("DaysUntilExpiryForDemo", 0)) <= 0
                ):
                    raise GatewayError("broker_demo_account_required")
        except (KeyError, TypeError):
            raise GatewayError("incomplete_broker_account") from None

    def price(self, item):
        # Tick history has venue timestamps; market-information prices do not.
        # Separate sides are usable only at the same tick; never invent a timestamp.
        sides = []
        for side in ("BID", "ASK"):
            data = fields(
                self.read(
                    f"/market/{item.market_id}/tickhistory",
                    query={
                        "PriceTicks": 1,
                        "priceType": side,
                        "ClientAccountId": self.config.client_account_id,
                    },
                    legacy=True,
                )
            )
            try:
                tick = fields(data["PriceTicks"][-1])
                sides.append((number(tick["Price"], positive=True), broker_time(tick["TickDate"])))
            except (KeyError, IndexError, TypeError):
                raise GatewayError("incomplete_rest_quote") from None
        if sides[0][1] != sides[1][1]:
            raise GatewayError("incoherent_rest_quote")
        return {
            "MarketId": item.market_id,
            "Bid": str(sides[0][0]),
            "Offer": str(sides[1][0]),
            "TickDate": stamp(sides[0][1]),
            "StatusSummary": 0,
        }

    def snapshots(self):
        as_of = stamp(self.now())  # Conservative request-start time, not last price time.
        self.protections = None
        margin = fields(
            self.read(
                "/v2/margin/clientAccountMargin",
                query={"clientAccountId": self.config.client_account_id},
            )
        )
        data = fields(
            self.read(
                "/order/openpositions",
                legacy=True,
                query={"TradingAccountId": self.config.trading_account_id},
            )
        )
        try:
            account = {
                "schema_version": 1,
                "as_of": as_of,
                "currency": margin["CurrencyIsoCode"],
                "balance": float(number(margin["Cash"])),
                "equity": float(number(margin["NetEquity"])),
                "margin_used": float(number(margin["Margin"])),
                "margin_available": float(number(margin["TradableFunds"])),
            }
            if not re.fullmatch(r"[A-Z]{3}", account["currency"]):
                raise GatewayError("invalid_broker_currency")
            from pipmind.gateway.books import protection_from_row

            positions, protections, identifiers = [], [], set()
            for row in map(fields, data["OpenPositions"]):
                item = self.symbols.market(row["MarketId"])
                if row["TradingAccountId"] != self.config.trading_account_id:
                    raise GatewayError("broker_account_mismatch")
                if type(row["OrderId"]) is not int or row["OrderId"] in identifiers:
                    raise GatewayError("invalid_position_identity")
                identifiers.add(row["OrderId"])
                side = row["Direction"].lower()
                if side not in {"buy", "sell"}:
                    raise GatewayError("invalid_position_side")
                positions.append(
                    {
                        "instrument": item.instrument,
                        "side": side,
                        "size": float(
                            number(row["Quantity"], positive=True) / item.quantity_factor
                        ),
                        "average_price": float(number(row["Price"], positive=True))
                        if row["Price"] is not None
                        else None,
                    }
                )
                # Named protection fields only. A bad shape marks that row unknown
                # and does not drop the position or invent a stop/limit role.
                protections.append(protection_from_row(row))
        except (KeyError, TypeError, AttributeError):
            raise GatewayError("incomplete_broker_snapshot") from None
        self.protections = protections
        return account, {"schema_version": 1, "as_of": as_of, "positions": positions}

    def working_orders(self):
        """Active stop/limit orders. Read-only; this method cannot submit or change them."""
        from pipmind.gateway.books import normalize_working_orders

        data = fields(
            self.read(
                "/order/activestoplimitorders",
                legacy=True,
                query={"TradingAccountId": self.config.trading_account_id},
            )
        )
        rows = data.get("ActiveStopLimitOrders")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise GatewayError("incomplete_broker_orders")
        return normalize_working_orders(rows, self.symbols, self.config.trading_account_id)

    def bar_history(self, item, interval, span, to_timestamp, max_results):
        """Read-only OHLC bars. This method cannot submit or change an order."""
        if (
            interval not in {"MINUTE", "HOUR", "DAY"}
            or type(span) is not int
            or span not in {1, 2, 3, 4, 5, 8, 10, 15, 30}
            or type(to_timestamp) is not int
            or to_timestamp <= 0
            or type(max_results) is not int
            or not 1 <= max_results <= 240
        ):
            raise GatewayError("invalid_bar_request")
        return self.read(
            f"/market/{item.market_id}/barhistorybefore",
            legacy=True,
            query={
                "interval": interval,
                "span": span,
                "toTimestampUTC": to_timestamp,
                "maxResults": max_results,
                "priceType": "MID",
            },
        )
