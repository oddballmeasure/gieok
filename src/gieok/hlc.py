"""Hybrid logical clock: milliseconds since the epoch in the high bits, a counter in the low 16, so
ordering follows wall time across hosts but never goes backwards on one, even if the clock does."""

import time

COUNTER_BITS = 16


class Clock:
    def __init__(self, last: int = 0):
        self.last = last

    def tick(self) -> int:
        now = int(time.time() * 1000) << COUNTER_BITS
        self.last = max(now, self.last + 1)
        return self.last

    def observe(self, remote: int) -> int:
        """Merge a timestamp seen from another node (Phase 3 sync)."""
        self.last = max(self.last, remote)
        return self.tick()


def wall_ms(hlc: int) -> int:
    return hlc >> COUNTER_BITS
