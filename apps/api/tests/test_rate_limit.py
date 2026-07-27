import pytest
from fastapi import HTTPException
from tripchord.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_in_memory_rate_limit_is_scoped_by_tenant_and_bucket() -> None:
    limiter = RateLimiter(limit=2, window_seconds=60)

    await limiter.check("tenant-a", "planning")
    await limiter.check("tenant-a", "planning")
    await limiter.check("tenant-b", "planning")
    await limiter.check("tenant-a", "offers")
    with pytest.raises(HTTPException) as exceeded:
        await limiter.check("tenant-a", "planning")

    assert exceeded.value.status_code == 429
    assert int(exceeded.value.headers["Retry-After"]) > 0
