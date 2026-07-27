from __future__ import annotations

import asyncio
from time import time

from fastapi import HTTPException, status
from redis.asyncio import Redis


class RateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        redis_url: str | None = None,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._redis = Redis.from_url(redis_url, decode_responses=True) if redis_url else None
        self._memory: dict[tuple[str, int], int] = {}
        self._lock = asyncio.Lock()

    async def check(self, tenant_id: str, bucket: str) -> None:
        window = int(time()) // self._window_seconds
        key = f"tripchord:rate:{bucket}:{tenant_id}:{window}"
        if self._redis is not None:
            count, ttl = await self._redis.eval(
                """
                local current = redis.call('INCR', KEYS[1])
                if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
                return {current, redis.call('TTL', KEYS[1])}
                """,
                1,
                key,
                self._window_seconds,
            )
            current = int(count)
            retry_after = max(1, int(ttl))
        else:
            async with self._lock:
                memory_key = (f"{bucket}:{tenant_id}", window)
                self._memory = {
                    existing_key: value
                    for existing_key, value in self._memory.items()
                    if existing_key[1] >= window - 1
                }
                current = self._memory.get(memory_key, 0) + 1
                self._memory[memory_key] = current
                retry_after = self._window_seconds - int(time()) % self._window_seconds
        if current > self._limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded for this tenant and operation",
                headers={"Retry-After": str(retry_after)},
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "in-memory-single-process"
