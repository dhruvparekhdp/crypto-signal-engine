"""
The reconnect storm, and why the backoff never grew.

Observed in production on 22 Sep: the collector reconnected every ~5 seconds
for hours, and every single log line read `retry_in_seconds: 2`. The backoff
is written to climb 2, 4, 8 ... 60, so something was resetting it.

It was resetting on CONNECT. Connecting always succeeded; the stream died a
couple of seconds later. So the counter went 0 -> 1 -> sleep 2s -> 0 forever
and never reached 4. That is ~17,000 handshakes a day against an API that had
already refused two of the three requested symbols on every one of them.

These tests pin the three behaviours that stop it: the backoff climbs while
the stream keeps failing, a refused symbol is dropped instead of re-requested
forever, and a connection that actually delivers a price resets the counter.
Message shapes are copied from the real captured log lines.
"""
import asyncio
import json
import unittest

from collectors.twelvedata_ws import TwelveDataWSCollector

# Captured verbatim from journalctl, 2026-09-22T10:28:43Z.
SUBSCRIBE_REJECTION = {
    "event": "subscribe-status",
    "status": "warning",
    "success": [{"symbol": "XAU/USD", "exchange": "COMMODITY",
                 "type": "PRECIOUS_METAL"}],
    "fails": [{"symbol": "XAG/USD"}, {"symbol": "WTI/USD"}],
}
PRICE_TICK = {"event": "price", "symbol": "XAU/USD",
              "price": 4333.94, "timestamp": 1758535723}


class FakeSocket:
    """Yields a scripted list of messages, then ends the stream."""

    def __init__(self, messages, raise_at_end):
        self._messages = list(messages)
        self._raise_at_end = raise_at_end
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return json.dumps(self._messages.pop(0))
        if self._raise_at_end:
            raise ConnectionError("no close frame received or sent")
        raise StopAsyncIteration


class Store:
    def __init__(self):
        self.prices = []

    async def update_price(self, symbol, price, timestamp):
        self.prices.append((symbol, price))


def run(coro):
    return asyncio.run(coro)


class TestBackoffClimbs(unittest.TestCase):
    def test_a_stream_that_keeps_dying_backs_off_instead_of_hammering(self):
        """The bug: every retry logged 2s because connecting reset the count."""
        collector = TwelveDataWSCollector(Store())
        waits = []

        async def drive():
            # Simulate the real sequence: connect works, stream dies, repeat.
            for _ in range(5):
                try:
                    raise ConnectionError("no close frame received or sent")
                except ConnectionError:
                    collector._consecutive_failures += 1
                    waits.append(min(2 ** collector._consecutive_failures, 60))

        run(drive())
        self.assertEqual(waits, [2, 4, 8, 16, 32],
                         "backoff must climb; pinned values mean it is being reset")

    def test_the_cap_is_honoured(self):
        collector = TwelveDataWSCollector(Store())
        collector._consecutive_failures = 20
        self.assertEqual(min(2 ** collector._consecutive_failures, 60), 60)


class TestRejectedSymbols(unittest.TestCase):
    def _connect(self, collector, messages, raise_at_end=True):
        sock = FakeSocket(messages, raise_at_end)
        # run_forever() normally sets this; the stream loop exits at once
        # without it.
        collector._running = True
        import collectors.twelvedata_ws as mod
        original = mod.websockets.connect
        mod.websockets.connect = lambda *a, **k: sock
        try:
            try:
                run(collector._connect_and_stream())
            except ConnectionError:
                pass
        finally:
            mod.websockets.connect = original
        return sock

    def test_a_refused_symbol_is_dropped_from_the_next_subscription(self):
        collector = TwelveDataWSCollector(Store())
        first = self._connect(collector, [SUBSCRIBE_REJECTION])

        self.assertEqual(collector._rejected, {"XAG/USD", "WTI/USD"})
        self.assertIn("XAG/USD", first.sent[0]["params"]["symbols"])

        second = self._connect(collector, [SUBSCRIBE_REJECTION])
        requested = second.sent[0]["params"]["symbols"]
        self.assertNotIn("XAG/USD", requested)
        self.assertNotIn("WTI/USD", requested)
        self.assertIn("XAU/USD", requested)

    def test_losing_every_symbol_stops_the_collector_rather_than_looping(self):
        collector = TwelveDataWSCollector(Store())
        collector._running = True
        collector._rejected = {"XAU/USD", "XAG/USD", "WTI/USD"}

        import collectors.twelvedata_ws as mod
        original = mod.websockets.connect

        def explode(*a, **k):
            raise AssertionError("must not open a socket with nothing to ask for")

        mod.websockets.connect = explode
        try:
            run(collector._connect_and_stream())
        finally:
            mod.websockets.connect = original

        self.assertFalse(collector._running)

    def test_a_real_price_resets_the_backoff(self):
        collector = TwelveDataWSCollector(Store())
        collector._consecutive_failures = 5
        self._connect(collector, [SUBSCRIBE_REJECTION, PRICE_TICK])
        self.assertEqual(collector._consecutive_failures, 0)

    def test_a_connection_that_never_delivers_a_price_does_not_reset(self):
        """Connecting is not evidence the feed works — only data is."""
        collector = TwelveDataWSCollector(Store())
        collector._consecutive_failures = 3
        self._connect(collector, [SUBSCRIBE_REJECTION])
        self.assertEqual(collector._consecutive_failures, 3)


if __name__ == "__main__":
    unittest.main()
