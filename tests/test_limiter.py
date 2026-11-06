import asyncio
import time

from app.llm import Limiter


async def test_concurrent_acquires_are_spaced_by_interval():
    limiter = Limiter(rpm=1200)  # 50 ms interval
    starts = []

    async def call():
        await limiter.acquire()
        starts.append(time.monotonic())

    await asyncio.gather(*(call() for _ in range(8)))
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert len(starts) == 8
    assert all(gap >= 0.05 - 0.002 for gap in gaps), gaps


async def test_idle_limiter_does_not_wait():
    limiter = Limiter(rpm=1)  # 60 s interval
    started = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - started < 0.05
