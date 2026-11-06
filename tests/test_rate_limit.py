import pytest

from app import webhook
from app.webhook import RateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_burst_passes_then_excess_waits():
    limiter = RateLimiter(per_minute=60, burst=3, clock=Clock())
    assert [limiter.take("1.1.1.1") for _ in range(3)] == [0, 0, 0]
    assert limiter.take("1.1.1.1") == pytest.approx(1.0)  # one token per second


def test_tokens_refill_over_time():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=2, clock=clock)
    limiter.take("ip")
    limiter.take("ip")
    clock.now += 0.5
    assert limiter.take("ip") == pytest.approx(0.5)
    clock.now += 0.5
    assert limiter.take("ip") == 0
    clock.now += 3600  # refill never exceeds the burst
    assert [limiter.take("ip") for _ in range(3)][-1] > 0


def test_ips_are_independent():
    limiter = RateLimiter(per_minute=60, burst=1, clock=Clock())
    assert limiter.take("a") == 0
    assert limiter.take("a") > 0
    assert limiter.take("b") == 0


@pytest.mark.parametrize(("per_minute", "burst"), [(0, 200), (600, 0)])
def test_zero_disables(per_minute, burst):
    limiter = RateLimiter(per_minute=per_minute, burst=burst, clock=Clock())
    assert all(limiter.take("ip") == 0 for _ in range(1000))
    assert not limiter.buckets


def test_tracked_ips_are_capped_and_stalest_is_evicted():
    limiter = RateLimiter(per_minute=60, burst=1, max_clients=2, clock=Clock())
    limiter.take("a")
    limiter.take("b")
    limiter.take("a")  # a is now more recent than b
    limiter.take("c")
    assert list(limiter.buckets) == ["a", "c"]
    for i in range(100):
        limiter.take(f"ip-{i}")
    assert len(limiter.buckets) == 2


PAYLOAD = {
    "event_id": "evt-rl",
    "ticket_id": "T-1",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "https://helpdesk.example.com/tickets/T-1/replies",
}


async def test_webhook_answers_429_with_retry_after(client, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(webhook, "limiter", RateLimiter(per_minute=30, burst=2, clock=clock))
    codes = [(await client.post("/webhooks/helpdesk", json=PAYLOAD)).status_code for _ in range(2)]
    assert codes == [200, 200]

    r = await client.post("/webhooks/helpdesk", json=PAYLOAD)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "2"  # 30/min: next token in 2 s
    assert r.json() == {"detail": "Too many requests"}

    clock.now += 2
    assert (await client.post("/webhooks/helpdesk", json=PAYLOAD)).status_code == 200


async def test_other_endpoints_are_not_limited(client, monkeypatch):
    monkeypatch.setattr(webhook, "limiter", RateLimiter(per_minute=1, burst=1, clock=Clock()))
    codes = [(await client.get("/health")).status_code for _ in range(5)]
    assert codes == [200] * 5
