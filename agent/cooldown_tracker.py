"""Provider cooldown tracker.

Tracks per-(provider, model) cooldowns after failures and supports automatic
duration escalation when the same model keeps failing.
"""

from __future__ import annotations

import enum
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class CooldownReason(str, enum.Enum):
    """Enumeration of failure reasons with associated cooldown durations."""

    RATE_LIMIT = "rate_limit"
    """HTTP 429 / rate-limited. Cooldown: 30 s."""

    OVERLOAD = "overload"
    """Provider overloaded / capacity-exceeded. Cooldown: 60 s."""

    AUTH_FAILURE = "auth_failure"
    """Authentication or authorisation error. Cooldown: 600 s (10 min)."""

    PERMANENT_AUTH = "permanent_auth"
    """Permanent auth failure (invalid key, revoked token). Cooldown: 3600 s (1 h)."""

    SERVER_ERROR = "server_error"
    """Transient server-side error (5xx). Cooldown: 30 s."""

    TIMEOUT = "timeout"
    """Request timed out. Cooldown: 30 s."""

    CONTEXT_LENGTH = "context_length"
    """Context-length exceeded — retry immediately. Cooldown: 0 s."""

    UNKNOWN = "unknown"
    """Unclassified failure. Cooldown: 30 s."""


# Maps each CooldownReason to its base cooldown duration in seconds.
_COOLDOWN_DURATIONS: Dict[CooldownReason, float] = {
    CooldownReason.RATE_LIMIT: 30.0,
    CooldownReason.OVERLOAD: 60.0,
    CooldownReason.AUTH_FAILURE: 600.0,  # 10 min
    CooldownReason.PERMANENT_AUTH: 3600.0,  # 1 h
    CooldownReason.SERVER_ERROR: 30.0,
    CooldownReason.TIMEOUT: 30.0,
    CooldownReason.CONTEXT_LENGTH: 0.0,
    CooldownReason.UNKNOWN: 30.0,
}

# Maximum cooldown duration after escalation (seconds).
_MAX_COOLDOWN: float = 3600.0  # 1 h


class CooldownTracker:
    """Per-(provider, model) cooldown state with exponential escalation.

    Every recorded failure applies a cooldown whose base duration is determined
    by *reason*.  If the same ``"provider:model"`` key fails repeatedly its
    cooldown is escalated: x2, x4, x8 … (capped at 1 hour).

    Usage
    -----
    >>> tracker = CooldownTracker()
    >>> tracker.record_failure("openai", "gpt-4", "rate_limit")
    >>> tracker.is_on_cooldown("openai", "gpt-4")
    True
    >>> tracker.get_cooldown_remaining("openai", "gpt-4")
    29.5  # (roughly)
    """

    probe_intervals: List[float] = [0.1, 0.5, 1.0, 2.0, 5.0]
    """Intervals (in seconds) to wait before re-probing a model that is cooling
    down.  The caller is expected to iterate through these values."""

    def __init__(self) -> None:
        # "provider:model" -> (reason, end_timestamp)
        self._cooldowns: Dict[str, Tuple[str, float]] = {}
        # "provider:model" -> consecutive failure count (for escalation)
        self._escalation_count: Dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_failure(
        self,
        provider: str,
        model: str,
        reason: str,
    ) -> None:
        """Record a failure for *provider*:*model* and apply a cooldown.

        Parameters
        ----------
        provider:
            The provider name (e.g. ``"openai"``, ``"anthropic"``).
        model:
            The model identifier (e.g. ``"gpt-4"``, ``"claude-3-opus"``).
        reason:
            A string matching one of the :class:`CooldownReason` values.
            Unknown strings are treated as ``CooldownReason.UNKNOWN``.
        """
        key = self._make_key(provider, model)

        try:
            base = _COOLDOWN_DURATIONS[CooldownReason(reason)]
        except (ValueError, KeyError):
            base = _COOLDOWN_DURATIONS[CooldownReason.UNKNOWN]

        # Escalate for repeated failures.
        self._escalation_count[key] += 1
        n = self._escalation_count[key]
        multiplier = 2 ** (n - 1)  # 1, 2, 4, 8, …
        duration = min(base * multiplier, _MAX_COOLDOWN)

        end_timestamp = time.monotonic() + duration
        self._cooldowns[key] = (reason, end_timestamp)

    def is_on_cooldown(self, provider: str, model: str) -> bool:
        """Check whether *provider*:*model* is currently in cooldown.

        Parameters
        ----------
        provider:
            Provider name.
        model:
            Model identifier.

        Returns
        -------
            ``True`` if the cooldown period has not yet expired.
        """
        remaining = self._get_remaining(provider, model)
        return remaining > 0.0

    def get_cooldown_remaining(self, provider: str, model: str) -> float:
        """Return the remaining cooldown duration in seconds.

        Parameters
        ----------
        provider:
            Provider name.
        model:
            Model identifier.

        Returns
        -------
            Seconds remaining (may be 0.0 if no cooldown is active or
            it has already expired).
        """
        return self._get_remaining(provider, model)

    def get_cooldown_reason(self, provider: str, model: str) -> str:
        """Return the reason associated with the current cooldown.

        Returns an empty string when no cooldown is active.

        Parameters
        ----------
        provider:
            Provider name.
        model:
            Model identifier.
        """
        key = self._make_key(provider, model)
        entry = self._cooldowns.get(key)
        if entry is not None:
            reason, end_ts = entry
            if time.monotonic() < end_ts:
                return reason
        return ""

    def clear(self, provider: str, model: str) -> None:
        """Reset the cooldown and escalation counter for *provider*:*model*.

        Parameters
        ----------
        provider:
            Provider name.
        model:
            Model identifier.
        """
        key = self._make_key(provider, model)
        self._cooldowns.pop(key, None)
        self._escalation_count.pop(key, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_remaining(self, provider: str, model: str) -> float:
        key = self._make_key(provider, model)
        entry = self._cooldowns.get(key)
        if entry is None:
            return 0.0
        _, end_ts = entry
        remaining = end_ts - time.monotonic()
        if remaining <= 0.0:
            # Clean up expired entries lazily.
            del self._cooldowns[key]
            self._escalation_count.pop(key, None)
            return 0.0
        return remaining

    @staticmethod
    def _make_key(provider: str, model: str) -> str:
        return f"{provider}:{model}"
