"""Private configuration and exact broker-unit mapping."""

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from urllib.parse import urlsplit

from pipmind.contracts import ContractError, loads


class GatewayError(ContractError):
    """A fixed, payload-free failure code safe for operational feedback."""


def number(value, *, positive=False):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or len(result.as_tuple().digits) > 28
            or (positive and result <= 0)
            or (result != 0 and not -28 <= result.adjusted() <= 28)
        ):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise GatewayError("invalid_broker_number") from None


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"\s|[\x00-\x1f]", value):
        raise GatewayError("https_broker_endpoint_required")
    try:
        parts = urlsplit(value)
        port = parts.port
    except (ValueError, TypeError):
        raise GatewayError("https_broker_endpoint_required") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise GatewayError("https_broker_endpoint_required")
    return value.rstrip("/")


@dataclass(frozen=True)
class Instrument:
    instrument: str
    market_id: int
    unit: str
    quantity_factor: Decimal
    increment: Decimal
    minimum: Decimal
    stale_seconds: float = 15

    def quantity(self, size):
        with localcontext() as context:
            context.prec = 128  # Exact product/remainder for the bounded 28-digit operands.
            value = number(size, positive=True) * self.quantity_factor
            try:
                if value < self.minimum or value % self.increment:
                    raise InvalidOperation
            except InvalidOperation:
                raise GatewayError("unrepresentable_size") from None
        return value


class SymbolMap:
    def __init__(self, rows):
        if not isinstance(rows, list):
            raise GatewayError("invalid_symbol_map")
        self.by_symbol, self.by_market = {}, {}
        for row in rows:
            try:
                symbol, market = row["instrument"], row["market_id"]
                if not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", symbol):
                    raise ValueError
                if type(market) is not int or market <= 0 or not row["unit"]:
                    raise ValueError
                item = Instrument(
                    symbol,
                    market,
                    row["unit"],
                    number(row["quantity_factor"], positive=True),
                    number(row["increment"], positive=True),
                    number(row["minimum"], positive=True),
                    float(number(row.get("stale_seconds", 15), positive=True)),
                )
                if not 1 <= item.stale_seconds <= 300:
                    raise ValueError
                if symbol in self.by_symbol or market in self.by_market:
                    raise ValueError
                self.by_symbol[symbol], self.by_market[market] = item, item
            except (KeyError, TypeError, ValueError):
                raise GatewayError("invalid_symbol_map") from None
        if not 1 <= len(self.by_symbol) <= 50:
            raise GatewayError("invalid_symbol_map")

    def symbol(self, value):
        try:
            return self.by_symbol[value]
        except (KeyError, TypeError):
            raise GatewayError("unsupported_instrument") from None

    def market(self, value):
        if type(value) is not int or value not in self.by_market:
            raise GatewayError("unknown_broker_market")
        return self.by_market[value]


@dataclass(repr=False)
class BrokerConfig:
    api_root: str
    legacy_root: str
    stream_url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    app_key: str = field(repr=False)
    client_account_id: int
    trading_account_id: int
    environment: str

    def __post_init__(self):
        for value in (self.api_root, self.legacy_root, self.stream_url):
            endpoint(value)
        if (
            self.environment not in {"demo", "live"}
            or not all((self.username, self.password, self.app_key))
            or type(self.client_account_id) is not int
            or type(self.trading_account_id) is not int
            or min(self.client_account_id, self.trading_account_id) <= 0
        ):
            raise GatewayError("invalid_broker_configuration")

    @classmethod
    def from_env(cls):
        try:
            return cls(
                *[
                    os.environ[f"PIPMIND_BROKER_{key}"]
                    for key in (
                        "API_ROOT",
                        "LEGACY_ROOT",
                        "STREAM_URL",
                        "USERNAME",
                        "PASSWORD",
                        "APP_KEY",
                    )
                ],
                int(os.environ["PIPMIND_BROKER_CLIENT_ACCOUNT_ID"]),
                int(os.environ["PIPMIND_BROKER_TRADING_ACCOUNT_ID"]),
                os.environ["PIPMIND_BROKER_ENVIRONMENT"],
            )
        except (KeyError, ValueError):
            raise GatewayError("missing_broker_configuration") from None


def symbols_from_env():
    try:
        path = Path(os.environ["PIPMIND_GATEWAY_SYMBOLS"])
        if path.stat().st_size > 65_536:
            raise GatewayError("symbol_map_too_large")
        value = loads(path.read_bytes())
        # Object wrapper keeps JSON arrays literal through ARM's generated
        # resource expressions. Local/M2 array files remain compatible.
        if isinstance(value, dict):
            if set(value) != {"symbols"}:
                raise GatewayError("invalid_symbol_map")
            value = value["symbols"]
        return SymbolMap(value)
    except (KeyError, OSError):
        raise GatewayError("missing_symbol_map") from None
