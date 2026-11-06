import asyncio
import time

from app.config import settings


class Limiter:
    """Spaces request starts at least 60 / rpm seconds apart, so any 60 s window holds at most
    rpm starts. In-process: correct only with one worker replica."""

    def __init__(self, rpm: int) -> None:
        self.interval = 60 / rpm
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


limiter = Limiter(settings.llm_rpm)
