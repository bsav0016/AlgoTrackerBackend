"""Shared fakes for the test suite. Nothing here talks to a real service."""
import datetime
import json
from unittest import mock


class FakeResponse:
    """Stand-in for a `requests.Response` returned by the (mocked) TwelveData API."""

    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def twelve_data_values(first_date, closes):
    """Builds a TwelveData `values` payload: one daily bar per close, newest first (as the API sends it)."""
    first = datetime.date.fromisoformat(first_date)
    bars = [
        {
            "datetime": (first + datetime.timedelta(days=i)).isoformat(),
            "close": str(close),
            "low": str(close - 1),
            "high": str(close + 1),
        }
        for i, close in enumerate(closes)
    ]
    return bars[::-1]


class FakeRedis:
    """Minimal in-memory Redis for the commands RedisManager uses. Like redis-py (no decode_responses),
    it hands values back as bytes."""

    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, seconds, value):
        self.values[key] = str(value).encode()
        self.ttls[key] = seconds

    def set(self, key, value):
        self.values[key] = value if isinstance(value, bytes) else str(value).encode()

    def get(self, key):
        return self.values.get(key)

    def incr(self, key):
        count = int(self.values.get(key, b"0")) + 1
        self.values[key] = str(count).encode()
        return count

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    def ttl(self, key):
        return self.ttls.get(key, -2)

    # List commands, for the candle store. Index 0 is the head, as in Redis.
    def lpush(self, key, value):
        items = self.values.setdefault(key, [])
        items.insert(0, value if isinstance(value, bytes) else str(value).encode())
        return len(items)

    def ltrim(self, key, start, stop):
        self.values[key] = self.values.get(key, [])[start:stop + 1]

    def lrange(self, key, start, stop):
        items = self.values.get(key, [])
        return items[start:] if stop == -1 else items[start:stop + 1]

    def lindex(self, key, index):
        items = self.values.get(key, [])
        return items[index] if -len(items) <= index < len(items) else None


def ledger():
    """Ledger rows written by the code under test: everything except the opening-balance rows that every new
    user starts with."""
    from api.models import CreditTransaction
    return CreditTransaction.objects.exclude(idempotency_key__startswith="opening:")


class FakeExpo:
    """Stands in for Expo's push API at the HTTP edge, so the real client, batching and ticket parsing run.

    `sent` records `(token, body)` for each message Expo accepted a request for; `payloads` the matching `data`.
    `errors` maps a token to the ticket error Expo should answer with (for example `DeviceNotRegistered`);
    `fail_request` makes the whole request fail like a dropped connection.
    """

    def __init__(self):
        self.sent = []
        self.payloads = []
        self.requests = 0
        self.errors = {}
        self.fail_request = None

    def install(self, testcase):
        from api.services import notification_service
        patcher = mock.patch.object(notification_service.session, "post", side_effect=self.post)
        patcher.start()
        testcase.addCleanup(patcher.stop)
        return self

    def post(self, url, data=None, timeout=None):
        self.requests += 1
        if self.fail_request is not None:
            raise self.fail_request
        tickets = []
        for message in json.loads(data):
            error = self.errors.get(message["to"])
            if error:
                tickets.append({"status": "error", "message": error, "details": {"error": error}})
            else:
                self.sent.append((message["to"], message["body"]))
                self.payloads.append(message.get("data"))
                tickets.append({"status": "ok", "id": f"ticket-{len(self.sent)}"})
        return FakeResponse(200, {"data": tickets})
