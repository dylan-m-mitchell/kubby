"""Shared test helpers (importable by every test module under tests/)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll until *predicate* holds (workers finish off the UI thread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()
