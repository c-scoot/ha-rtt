"""Test the integration's local request ceilings and server-side deferrals."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.realtime_trains.budget import BudgetDeferred, RequestBudget

START = datetime(2026, 11, 20, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("ceiling", "spacing"),
    [(10, 2), (100, 31), (1000, 80)],
    ids=["minute", "hour", "day"],
)
async def test_local_rolling_ceilings_survive_restart(ceiling, spacing):
    """Space traffic far enough apart that only the intended window binds."""
    budget = RequestBudget()
    step = timedelta(seconds=spacing)
    for index in range(ceiling):
        await budget.async_acquire(now=START + index * step)

    restored = RequestBudget(json.loads(json.dumps(budget.dump())))
    attempt = START + ceiling * step
    with pytest.raises(BudgetDeferred) as failure:
        await restored.async_acquire(now=attempt)
    assert failure.value.next_attempt > attempt
    await restored.async_acquire(now=failure.value.next_attempt)


async def test_concurrent_requests_cannot_pass_minute_ceiling():
    """Multiple tracks contending for the same token share the minute limit."""
    budget = RequestBudget()
    results = await asyncio.gather(
        *(budget.async_acquire(now=START) for _ in range(15)), return_exceptions=True
    )
    assert sum(isinstance(result, BudgetDeferred) for result in results) == 5
    assert sum(not isinstance(result, Exception) for result in results) == 10


async def test_server_week_ceiling_is_persisted():
    """A low remaining week budget caused by other users of the token is honored."""
    budget = RequestBudget()
    budget.observe(
        {"x-ratelimit-limit-week": "10000", "X-RateLimit-Remaining-Week": "0"},
        now=START,
    )
    budget = RequestBudget(json.loads(json.dumps(budget.dump())))
    with pytest.raises(BudgetDeferred):
        await budget.async_acquire(now=START + timedelta(hours=1))
    await budget.async_acquire(now=START + timedelta(days=7, seconds=1))


async def test_local_week_ceiling_after_restoring_older_history():
    """Exercise the local week cap independently of the tighter subweek caps."""
    older_attempt = START - timedelta(days=2)
    budget = RequestBudget({"requests": [older_attempt.timestamp()] * 10000})

    with pytest.raises(BudgetDeferred) as failure:
        await budget.async_acquire(now=START)
    assert failure.value.next_attempt == older_attempt + timedelta(days=7)
    await budget.async_acquire(now=failure.value.next_attempt)


async def test_retry_after_and_server_headroom_are_shared():
    budget = RequestBudget()
    budget.observe({"Retry-After": "120", "X-RateLimit-Remaining-Hour": "1"}, now=START)

    with pytest.raises(BudgetDeferred) as failure:
        await budget.async_acquire(now=START + timedelta(seconds=119))
    assert failure.value.next_attempt == START + timedelta(seconds=120)
    now = failure.value.next_attempt
    await budget.async_acquire(now=now)
    with pytest.raises(BudgetDeferred):
        await budget.async_acquire(now=now)
