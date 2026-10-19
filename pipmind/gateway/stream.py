"""Official Lightstreamer SDK bridge; callbacks enqueue bounded data, never do I/O."""

from queue import Empty, Full, Queue

from pipmind.gateway.config import GatewayError

FIELDS = ["MarketId", "Bid", "Offer", "TickDate", "StatusSummary", "AuditId"]


class LightstreamerPrices:
    def __init__(self, broker, *, client_factory=None, subscription_factory=None):
        if client_factory is None or subscription_factory is None:
            from lightstreamer.client import LightstreamerClient, Subscription

            client_factory, subscription_factory = LightstreamerClient, Subscription
        self.broker = broker
        self.client_factory, self.subscription_factory = client_factory, subscription_factory
        self.queue, self.client, self.subscriptions = Queue(maxsize=1000), None, []
        self.epoch, self.overflow, self.generation = 0, False, None

    def _enqueue(self, epoch, value):
        try:
            self.queue.put_nowait((epoch, value))
        except Full:
            self.overflow = True

    def start(self):
        self.stop()
        session = self.broker.session()
        self.generation = self.broker.generation
        self.epoch += 1
        epoch, owner = self.epoch, self
        self.client = self.client_factory(self.broker.config.stream_url, "STREAMINGALL")
        self.client.connectionDetails.setUser(self.broker.config.username)
        self.client.connectionDetails.setPassword(session)
        # SDK handles transport recovery; the watchdog handles quiet data.
        self.client.connectionOptions.setForcedTransport("HTTP")

        from lightstreamer.client import ClientListener, SubscriptionListener

        class ClientEvents(ClientListener):
            def onServerError(self, code, message):
                owner._enqueue(epoch, {"error": "stream_auth" if code == 1 else "stream_error"})

        class PriceEvents(SubscriptionListener):
            def onItemUpdate(self, update):
                owner._enqueue(epoch, {field: update.getValue(field) for field in FIELDS})

            def onSubscriptionError(self, code, message):
                owner._enqueue(epoch, {"error": "stream_subscription_error"})

        self.client.addListener(ClientEvents())
        for item in self.broker.symbols.by_symbol.values():
            subscription = self.subscription_factory("MERGE", [f"ID.{item.market_id}"], FIELDS)
            subscription.setDataAdapter("PRICES")
            subscription.setRequestedSnapshot("yes")
            subscription.addListener(PriceEvents())
            self.client.subscribe(subscription)
            self.subscriptions.append(subscription)
        self.client.connect()

    def drain(self):
        if self.overflow:
            self.overflow = False
            # Clear the bounded transport buffer on a declared loss. Leaving
            # it full would make every subsequent callback set overflow again,
            # so a slow REST read could wedge the stream permanently.
            for _ in range(self.queue.maxsize):
                try:
                    self.queue.get_nowait()
                except Empty:
                    break
            raise GatewayError("stream_queue_overflow")
        values = []
        for _ in range(1000):
            try:
                epoch, value = self.queue.get_nowait()
            except Empty:
                break
            if epoch == self.epoch:
                values.append(value)
        return values

    def stop(self):
        self.epoch += 1  # In-flight callbacks from an old connection cannot revive prices.
        if self.client:
            for subscription in self.subscriptions:
                self.client.unsubscribe(subscription)
            self.client.disconnect()
        self.client, self.subscriptions = None, []
