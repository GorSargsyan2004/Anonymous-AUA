"""Anti-flood rate limiting service implementing a token-bucket algorithm per user.

Protects the bot and partner chats from rapid spam, bot floods, and script abuse
with burst allowance, warning debounce, and temporary cooldown penalties.
"""

import math
import time
from typing import Dict, List, Optional, Tuple


class UserBucket:
    """Tracks token bucket state and violation history for a single user."""

    def __init__(self, capacity: float) -> None:
        self.tokens: float = float(capacity)
        self.last_update: float = time.time()
        self.violations: List[float] = []
        self.cooldown_until: float = 0.0
        self.last_warning_sent: float = 0.0


class TokenBucketRateLimiter:
    """In-memory Token Bucket rate limiter with sliding window penalty tracking."""

    def __init__(
        self,
        capacity: float = 6.0,
        refill_rate: float = 2.0,
        penalty_threshold: int = 3,
        penalty_window: float = 60.0,
        cooldown_duration: float = 120.0,
        warning_debounce: float = 5.0,
    ) -> None:
        """Initialize the rate limiter configuration.

        Args:
            capacity: Max burst capacity (tokens).
            refill_rate: Tokens added per second.
            penalty_threshold: Number of rate violations within penalty_window to trigger cooldown.
            penalty_window: Time window in seconds to count violations.
            cooldown_duration: Cooldown duration in seconds when penalty is triggered.
            warning_debounce: Minimum seconds between warning messages sent back to user.
        """
        self.capacity: float = capacity
        self.refill_rate: float = refill_rate
        self.penalty_threshold: int = penalty_threshold
        self.penalty_window: float = penalty_window
        self.cooldown_duration: float = cooldown_duration
        self.warning_debounce: float = warning_debounce
        self._buckets: Dict[int, UserBucket] = {}

    def _get_or_create_bucket(self, user_id: int) -> UserBucket:
        if user_id not in self._buckets:
            self._buckets[user_id] = UserBucket(capacity=self.capacity)
        return self._buckets[user_id]

    def check(self, user_id: int, cost: float = 1.0) -> Tuple[bool, Optional[str], float, bool]:
        """Check whether a user is allowed to proceed with an action.

        Returns:
            Tuple of:
                - is_allowed (bool): True if allowed to proceed, False otherwise.
                - reason (str or None): "cooldown", "cooldown_triggered", "rate_exceeded", or None.
                - retry_after (float): Recommended wait time in seconds.
                - should_notify (bool): True if warning message should be sent to user (debounced).
        """
        now = time.time()
        bucket = self._get_or_create_bucket(user_id)

        # 1. Check if currently in cooldown
        if now < bucket.cooldown_until:
            retry_after = bucket.cooldown_until - now
            should_notify = (now - bucket.last_warning_sent) >= self.warning_debounce
            if should_notify:
                bucket.last_warning_sent = now
            return False, "cooldown", max(1.0, retry_after), should_notify

        # 2. Refill tokens
        elapsed = now - bucket.last_update
        bucket.last_update = now
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_rate)

        # 3. Check token availability
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True, None, 0.0, False

        # 4. Rate exceeded: record violation in sliding window
        # Clean older violations outside window
        window_start = now - self.penalty_window
        bucket.violations = [t for t in bucket.violations if t >= window_start]
        bucket.violations.append(now)

        # 5. Check if threshold exceeded -> trigger cooldown
        if len(bucket.violations) >= self.penalty_threshold:
            bucket.cooldown_until = now + self.cooldown_duration
            bucket.tokens = 0.0
            bucket.violations.clear()
            should_notify = (now - bucket.last_warning_sent) >= self.warning_debounce
            bucket.last_warning_sent = now
            return False, "cooldown_triggered", self.cooldown_duration, should_notify

        # Normal rate limit violation without cooldown
        missing_tokens = cost - bucket.tokens
        retry_after = math.ceil(missing_tokens / self.refill_rate)
        should_notify = (now - bucket.last_warning_sent) >= self.warning_debounce
        if should_notify:
            bucket.last_warning_sent = now

        return False, "rate_exceeded", max(1.0, float(retry_after)), should_notify

    def is_in_cooldown(self, user_id: int) -> Tuple[bool, float]:
        """Check if user is currently serving a cooldown penalty."""
        bucket = self._buckets.get(user_id)
        if not bucket:
            return False, 0.0
        now = time.time()
        if now < bucket.cooldown_until:
            return True, max(1.0, bucket.cooldown_until - now)
        return False, 0.0

    def reset(self, user_id: int) -> None:
        """Reset rate limiter state for a specific user."""
        if user_id in self._buckets:
            del self._buckets[user_id]

    def prune_stale(self, max_idle_seconds: float = 3600.0) -> int:
        """Prune inactive user buckets to prevent unbounded memory growth."""
        now = time.time()
        to_delete = [
            uid for uid, b in self._buckets.items()
            if (now - b.last_update) > max_idle_seconds and now >= b.cooldown_until
        ]
        for uid in to_delete:
            del self._buckets[uid]
        return len(to_delete)


# Global rate limiter instance
rate_limiter = TokenBucketRateLimiter()
