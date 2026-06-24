"""Tool loop detection module.

Detects repetitive tool-call patterns that indicate an agent is stuck in a loop.
Tracks calls per session, hashes arguments to identify duplicates, and escalates
from WARNING to CRITICAL as the repetition count grows.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Dict, List, Tuple


class ToolLoopDetector:
    """Detect tool-call loops per session.

    Records tool invocations and checks whether the same tool has been called
    with the same arguments repeatedly within a sliding window of recent turns.
    """

    DEFAULT_WINDOW: int = 10
    """Default number of recent turns to consider for loop detection."""

    # Internal type for a single tracked call entry.
    _CallEntry = Tuple[int, str, str]  # (turn_number, args_hash, result_hash)

    def __init__(self, window: int | None = None) -> None:
        """Initialize the detector.

        Parameters
        ----------
        window:
            Number of most recent turns to consider when checking for loops.
            Falls back to *DEFAULT_WINDOW* (10) when *None*.
        """
        self._window: int = window if window is not None else self.DEFAULT_WINDOW
        # session_id -> list of (turn_number, args_hash, result_hash)
        self._tracked_calls: Dict[str, List[self._CallEntry]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_call(
        self,
        session_id: str,
        turn_number: int,
        tool_name: str,
        args_dict: Dict[str, Any],
        result_dict: Dict[str, Any] | None = None,
    ) -> None:
        """Record a tool invocation for loop analysis.

        Parameters
        ----------
        session_id:
            Opaque identifier for the session the call belongs to.
        turn_number:
            Monotonically increasing turn counter for the session.
        tool_name:
            Name of the tool that was invoked.
        args_dict:
            Arguments passed to the tool (will be JSON-sorted-hashed).
        result_dict:
            Result returned by the tool (will be JSON-sorted-hashed).
            May be *None* when the result is unavailable.
        """
        args_hash = self._hash_dict(args_dict)
        result_hash = self._hash_dict(result_dict) if result_dict else ""
        self._tracked_calls[session_id].append(
            (turn_number, args_hash, result_hash)
        )

    def check_loop(
        self,
        session_id: str,
        tool_name: str,
        args_dict: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Check whether a tool call would form a loop.

        Analyses the recorded history for *session_id* and returns a
        ``(is_looping, reason)`` tuple.

        Detection rules (within the lookback window):
         - Same *tool_name* + same args_hash **>= 3 times** → ``WARNING``
         - Same *tool_name* + same args_hash **>= 5 times** → ``CRITICAL``

        Parameters
        ----------
        session_id:
            Session to check.
        tool_name:
            Name of the tool about to be called.
        args_dict:
            Arguments that would be passed to the tool.

        Returns
        -------
            ``(True, "WARNING: ...")`` or ``(True, "CRITICAL: ...")`` when
            a loop is detected; ``(False, "")`` otherwise.
        """
        args_hash = self._hash_dict(args_dict)
        calls = self._tracked_calls.get(session_id, [])
        if not calls:
            return False, ""

        # Prune history outside the lookback window.
        latest_turn = max(t for t, _, _ in calls)
        cutoff = latest_turn - self._window
        recent = [(t, a, r) for t, a, r in calls if t >= cutoff]

        # Count how many times the same (tool_name, args_hash) appears.
        count = sum(
            1 for _, a, _ in recent if a == args_hash
        )

        # The call being checked hasn't been recorded yet, so bump the count
        # conceptually to include the pending invocation.
        count += 1

        if count >= 5:
            return (
                True,
                f"CRITICAL: tool '{tool_name}' called with identical arguments "
                f"{count} times in the last {self._window} turns — interrupting.",
            )
        if count >= 3:
            return (
                True,
                f"WARNING: tool '{tool_name}' called with identical arguments "
                f"{count} times in the last {self._window} turns.",
            )

        return False, ""

    def cleanup(self, session_id: str) -> None:
        """Remove tracked data for a session that has ended.

        Parameters
        ----------
        session_id:
            Session to remove.
        """
        self._tracked_calls.pop(session_id, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_dict(data: Dict[str, Any]) -> str:
        """Return an MD5 hex digest of *data* sorted by its JSON keys.

        Two dictionaries with the same key-value pairs always produce the
        same hash regardless of insertion order.
        """
        serialised = json.dumps(
            data,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.md5(serialised.encode("utf-8")).hexdigest()
