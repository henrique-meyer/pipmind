"""Dry command gateway and per-instrument price failover, without trading policy."""

import hashlib
import json
from copy import deepcopy

from pipmind.contracts import Contracts, stamp, utc
from pipmind.gateway.ciapi import broker_time
from pipmind.gateway.config import GatewayError, number


class DryGateway:
    def __init__(self, symbols, ledger, now):
        self.symbols, self.ledger, self.now = symbols, ledger, now

    def _validate(self, request, db):
        item = self.symbols.symbol(request["instrument"])
        if request.get("size") is not None:
            item.quantity(request["size"])
        for field in ("stop", "limit"):
            if request.get(field) is not None:
                number(request[field], positive=True)
        related = request.get("related_client_order_id")
        if related:
            row = db.execute(
                "SELECT request, state, broker_id FROM commands WHERE command_id=?", (related,)
            ).fetchone()
            if row is None or json.loads(row[0])["instrument"] != item.instrument:
                raise GatewayError("unknown_related_command")
            if row[1] == "uncertain":
                raise GatewayError("uncertain_outcome_reconciliation_required")
            if not row[2]:
                raise GatewayError("related_command_has_no_broker_identifier")
        if request["action"] == "modify" and not related:
            raise GatewayError("related_command_required")

    def accept(self, request):
        return self.ledger.acknowledge(request, self.now(), self._validate)


class PriceRouter:
    def __init__(self, symbols, broker, now, *, resubscribe=lambda: None, contracts=None):
        self.symbols, self.broker, self.now = symbols, broker, now
        self.resubscribe = resubscribe
        self.contracts = contracts or Contracts()
        self.stream_times, self.latest, self.sources = {}, {}, {}
        self.last_rest, self.last_resubscribe = {}, None
        self.transitions, self.alerts = [], []

    def _price(self, item, raw, source):
        now, venue = self.now(), broker_time(raw["TickDate"])
        if venue > now:
            raise GatewayError("future_broker_quote")
        bid, ask = number(raw["Bid"], positive=True), number(raw["Offer"], positive=True)
        if bid > ask:
            raise GatewayError("crossed_broker_quote")
        stale = (now - venue).total_seconds() >= item.stale_seconds
        if source == "stream":
            stale |= str(raw.get("StatusSummary")) != "0"
        identity = f"{item.instrument}|{bid}|{ask}|{stamp(venue)}|{source}|{stale}"
        result = {
            "schema_version": 1,
            "event_id": hashlib.sha256(identity.encode()).hexdigest(),
            "instrument": item.instrument,
            "bid": float(bid),
            "ask": float(ask),
            "venue_ts": stamp(venue),
            "received_at": stamp(now),
            "source": source,
            "stale": stale,
        }
        self.contracts.validate("price-event", result)
        return result

    def _publish(self, event):
        symbol = event["instrument"]
        previous = self.latest.get(symbol)
        if previous and utc(event["venue_ts"]) < utc(previous["venue_ts"]):
            return None
        if self.sources.get(symbol) != event["source"]:
            self.sources[symbol] = event["source"]
            self.transitions.append(
                {"instrument": symbol, "source": event["source"], "ts": event["received_at"]}
            )
        self.latest[symbol] = deepcopy(event)
        return event

    def stream(self, raw):
        try:
            item = self.symbols.market(int(raw["MarketId"]))
            event = self._price(item, raw, "stream")
        except GatewayError:
            raise
        except (KeyError, TypeError, ValueError):
            raise GatewayError("invalid_stream_quote") from None
        if event["stale"]:
            return None  # A repeated old tick or closed market cannot recover the stream.
        previous = self.stream_times.get(item.instrument)
        venue = utc(event["venue_ts"])
        if previous is not None and venue <= previous:
            return None
        latest = self.latest.get(item.instrument)
        if latest and venue < utc(latest["venue_ts"]):
            return None
        self.stream_times[item.instrument] = venue
        return self._publish(event)

    def poll(self):
        now, events, stale = self.now(), [], False
        for item in self.symbols.by_symbol.values():
            last = self.stream_times.get(item.instrument)
            if last is not None and (now - last).total_seconds() < item.stale_seconds:
                continue
            stale = True
            previous = self.latest.get(item.instrument)
            if previous and not previous["stale"] and previous["source"] == "stream":
                event = {**previous, "stale": True, "received_at": stamp(now)}
                event["event_id"] = hashlib.sha256(
                    (event["event_id"] + ":stale").encode()
                ).hexdigest()
                self.latest[item.instrument] = event
                events.append(deepcopy(event))
            last_rest = self.last_rest.get(item.instrument)
            if last_rest is not None and (now - last_rest).total_seconds() < 5:
                continue
            self.last_rest[item.instrument] = now
            try:
                raw = self.broker.price(item)
                event = self._price(item, raw, "rest")
                published = self._publish(event)
                if published:
                    events.append(published)
            except GatewayError as error:
                self.alerts.append({"code": str(error), "ts": stamp(now)})
        if stale and (
            self.last_resubscribe is None or (now - self.last_resubscribe).total_seconds() >= 30
        ):
            self.last_resubscribe = now
            self.resubscribe()
        # Operational state is bounded; event persistence belongs to the future SQL outbox.
        self.transitions[:] = self.transitions[-100:]
        self.alerts[:] = self.alerts[-100:]
        return events

    def current(self):
        now = self.now()
        values = deepcopy(list(self.latest.values()))
        for value in values:
            item = self.symbols.symbol(value["instrument"])
            if (now - utc(value["venue_ts"])).total_seconds() >= item.stale_seconds:
                value["stale"] = True
        return values
