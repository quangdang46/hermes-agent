"""MemPalace memory plugin — MemoryProvider interface.

Persistent memory via the mempalace MCP server (``mpr mcp``). Organises
knowledge into a structured memory palace with tiered retrieval (hybrid
semantic + keyword + knowledge graph). Local-first, zero cloud dependency.

Architecture::

    Hermes Agent
      └── MemPalaceMemoryProvider  (this file)
            └── MemPalaceMCPClient  (mcp_client.py / JSON-RPC over stdio)
                  └── mpr mcp (subprocess, persistent)
                        └── PalaceDb (SQLite + vector index)
                              └── $HERMES_HOME/mempalace/.mempalace/

Lightweight init (no LLM, no embedding model)::

    mpr init <dir> --no-llm --search-strategy contains --yes

Config via environment variables:
  MEMPALACE_PALACE_PATH  — palace storage directory (default: $HERMES_HOME/mempalace/)
  MPR_PATH               — explicit path to mpr binary
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .mcp_client import MemPalaceMCPClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Timeouts
_INIT_TIMEOUT = 30      # mpr init subprocess
_PREFETCH_TIMEOUT = 10  # prefetch sync search
_TOOL_TIMEOUT = 15      # user-facing tool calls
_SYNC_TIMEOUT = 30      # background sync_turn

# Length thresholds (skip noise)
_MIN_QUERY_LEN = 10
_MIN_OUTPUT_LEN = 20
_MIN_SYNC_LEN = 10

# ---------------------------------------------------------------------------
# mpr binary resolution (cached, thread-safe — pattern from ByteRover)
# ---------------------------------------------------------------------------

_mpr_path_lock = threading.Lock()
_cached_mpr_path: Optional[str] = None


def _resolve_mpr_path() -> Optional[str]:
    """Find the mpr binary on PATH or well-known install locations."""
    global _cached_mpr_path
    with _mpr_path_lock:
        if _cached_mpr_path is not None:
            return _cached_mpr_path if _cached_mpr_path != "" else None

    # Check env override
    env_path = os.environ.get("MPR_PATH", "")
    if env_path:
        p = Path(env_path)
        if p.exists():
            found = str(p.resolve())
            with _mpr_path_lock:
                _cached_mpr_path = found
            return found

    # PATH search
    found = shutil.which("mpr")
    if not found:
        home = Path.home()
        candidates = [
            home / ".cargo" / "bin" / "mpr",
            home / ".local" / "bin" / "mpr",
            Path("/usr/local/bin/mpr"),
        ]
        for c in candidates:
            if c.exists():
                found = str(c)
                break

    with _mpr_path_lock:
        if _cached_mpr_path is not None:
            return _cached_mpr_path if _cached_mpr_path != "" else None
        _cached_mpr_path = found or ""
    return found


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": (
        "Search mempalace for past memories, decisions, patterns, "
        "and project knowledge using hybrid semantic + keyword search. "
        "Returns the most relevant results ranked by combined relevance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {
                "type": "integer",
                "description": "Max results (default 5, max 20).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

SAVE_SCHEMA = {
    "name": "memory_save",
    "description": (
        "Store important information, decisions, patterns, or facts "
        "in mempalace for long-term retention. "
        "Use for architectural decisions, bug fixes, user preferences, "
        "project conventions — anything worth remembering across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
            "room": {
                "type": "string",
                "description": "Category: decisions, patterns, facts, bugs, preferences (default: facts).",
                "default": "facts",
            },
        },
        "required": ["content"],
    },
}

STATUS_SCHEMA = {
    "name": "memory_status",
    "description": "Check mempalace status — connection health, database statistics.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class MemPalaceMemoryProvider(MemoryProvider):
    """MemPalace persistent memory via MCP protocol."""

    def __init__(self):
        self._mcp_client: Optional[MemPalaceMCPClient] = None
        self._mpr_path: Optional[str] = None
        self._palace_dir: str = ""
        self._session_id: str = ""
        self._turn_count: int = 0
        self._cron_skipped: bool = False
        self._agent_context: str = ""
        self._initialized: bool = False

    # -- Properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "mempalace"

    # -- Availability --------------------------------------------------------

    def is_available(self) -> bool:
        """Check if mpr binary is on PATH. No network calls, no side effects."""
        return _resolve_mpr_path() is not None

    # -- Configuration -------------------------------------------------------

    def get_config_schema(self):
        return [
            {
                "key": "palace_path",
                "description": "MemPalace palace directory path (default: $HERMES_HOME/mempalace/)",
                "env_var": "MEMPALACE_PALACE_PATH",
            },
            {
                "key": "auto_init",
                "description": "Auto-initialize palace if not found (default: true)",
                "default": True,
            },
        ]

    def save_config(self, values, hermes_home):
        """Write config to $HERMES_HOME/mempalace.json."""
        import json as _json
        config_path = Path(hermes_home) / "mempalace.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if config_path.exists():
            existing = _json.loads(config_path.read_text())
        existing.update(values)
        config_path.write_text(_json.dumps(existing, indent=2))

    # -- Lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize the mempalace provider for a Hermes session.

        1. Skip if cron/flush context (no memory operations needed)
        2. Resolve mpr binary
        3. Determine palace directory
        4. Auto-initialise palace if needed (``mpr init --light``)
        5. Start persistent MCP connection
        """
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")

        # Cron guard — skip memory in cron / flush contexts
        if agent_context in {"cron", "flush"} or platform == "cron":
            self._cron_skipped = True
            logger.info("MemPalace: skipped for cron context")
            return

        self._session_id = session_id
        self._agent_context = agent_context
        self._turn_count = 0

        # Resolve mpr binary
        mpr_path = _resolve_mpr_path()
        if not mpr_path:
            logger.warning("mpr binary not found — mempalace plugin inactive")
            return
        self._mpr_path = mpr_path

        # Determine palace directory
        hermes_home = kwargs.get("hermes_home", "")
        self._palace_dir = os.environ.get(
            "MEMPALACE_PALACE_PATH",
            str(Path(hermes_home) / "mempalace"),
        )

        # Auto-initialize palace if not found
        if not self._palace_init_check():
            logger.info("MemPalace: auto-initialising palace at %s", self._palace_dir)
            if not self._auto_init_palace():
                logger.warning("MemPalace: auto-init failed, plugin inactive")
                return

        # Start MCP client
        self._mcp_client = MemPalaceMCPClient(mpr_path, self._palace_dir)
        if not self._mcp_client.start(timeout=15):
            logger.warning("Failed to connect to mempalace MCP server")
            return

        self._initialized = True
        logger.info("MemPalace: initialised (session=%s)", session_id[:8])

    def shutdown(self) -> None:
        """Graceful shutdown — stop MCP client."""
        if self._mcp_client:
            self._mcp_client.stop()
            self._mcp_client = None
        self._initialized = False

    # -- System prompt -------------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._initialized or self._cron_skipped:
            return ""
        return (
            "# MemPalace Memory\n"
            "Active. Persistent cross-session memory with hybrid search "
            "(vector + BM25 + knowledge graph) and structured storage.\n"
            "Available tools:\n"
            "- memory_search: Search past memories by keyword or concept\n"
            "- memory_save: Store important information permanently\n"
            "- memory_status: Check mempalace health and statistics"
        )

    # -- Prefetch ------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Search mempalace before the agent's first LLM call.

        Blocks until the search completes (up to ``_PREFETCH_TIMEOUT`` s),
        returning formatted context text.
        """
        if not self._initialized or self._cron_skipped:
            return ""
        if not query or len(query.strip()) < _MIN_QUERY_LEN:
            return ""

        result = self._mcp_client.call_tool(
            "mempalace_hybrid_search",
            {"query": query.strip()[:5000], "limit": 5},
            timeout=_PREFETCH_TIMEOUT,
        )

        if result["success"] and result.get("text", "").strip():
            text = result["text"].strip()
            if len(text) > _MIN_OUTPUT_LEN:
                return f"## MemPalace Context\n{text}"
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No-op: prefetch() runs synchronously at turn start."""
        pass

    # -- Sync turn -----------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "", messages=None) -> None:
        """Store conversation turn in mempalace (background, non-blocking)."""
        if not self._initialized or self._cron_skipped:
            return
        if len(user_content.strip()) < _MIN_SYNC_LEN:
            return

        self._turn_count += 1

        def _sync():
            try:
                combined = (
                    f"User: {user_content[:2000]}\n"
                    f"Assistant: {assistant_content[:2000]}"
                )
                self._mcp_client.call_tool(
                    "mempalace_add_drawer",
                    {"wing": "hermes", "room": "turns", "content": combined},
                    timeout=_SYNC_TIMEOUT,
                )
            except Exception as e:
                logger.debug("MemPalace sync_turn failed: %s", e)

        t = threading.Thread(target=_sync, daemon=True, name="mempalace-sync")
        t.start()

    # -- Tool schemas --------------------------------------------------------

    def get_tool_schemas(self):
        return [SEARCH_SCHEMA, SAVE_SCHEMA, STATUS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "memory_search":
            return self._tool_search(args)
        elif tool_name == "memory_save":
            return self._tool_save(args)
        elif tool_name == "memory_status":
            return self._tool_status()
        return tool_error(f"Unknown tool: {tool_name}")

    # -- Lifecycle hooks -----------------------------------------------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata=None) -> None:
        """Mirror built-in memory writes to mempalace."""
        if not self._initialized or self._cron_skipped:
            return
        if action not in {"add", "replace"} or not content:
            return

        def _write():
            try:
                label = "User profile" if target == "user" else "Agent memory"
                self._mcp_client.call_tool(
                    "mempalace_add_drawer",
                    {"wing": "hermes", "room": target,
                     "content": f"[{label}] {content}"},
                    timeout=_SYNC_TIMEOUT,
                )
            except Exception as e:
                logger.debug("MemPalace memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="mempalace-memwrite")
        t.start()

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Save context before compression discards turns."""
        if not self._initialized or self._cron_skipped:
            return ""
        if not messages:
            return ""

        # Build a summary of messages about to be compressed
        parts = []
        for msg in messages[-10:]:  # last 10 messages
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip() and role in {"user", "assistant"}:
                parts.append(f"{role}: {content[:500]}")

        if not parts:
            return ""

        combined = "\n".join(parts)

        def _flush():
            try:
                self._mcp_client.call_tool(
                    "mempalace_add_drawer",
                    {"wing": "hermes", "room": "compression",
                     "content": combined},
                    timeout=_SYNC_TIMEOUT,
                )
            except Exception as e:
                logger.debug("MemPalace pre-compression flush failed: %s", e)

        t = threading.Thread(target=_flush, daemon=True, name="mempalace-flush")
        t.start()
        return ""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Session end — flush final messages to mempalace."""
        if not self._initialized or self._cron_skipped:
            return
        if not messages:
            return

        def _flush():
            try:
                parts = []
                for msg in messages[-5:]:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        parts.append(f"{role}: {content[:1000]}")

                if parts:
                    combined = "\n".join(parts)
                    self._mcp_client.call_tool(
                        "mempalace_add_drawer",
                        {"wing": "hermes", "room": "sessions",
                         "content": f"[Session {self._session_id[:8]}]\n{combined}"},
                        timeout=_SYNC_TIMEOUT,
                    )
            except Exception as e:
                logger.debug("MemPalace session-end flush failed: %s", e)

        t = threading.Thread(target=_flush, daemon=True, name="mempalace-sessend")
        t.start()

    # -- Tool implementations ------------------------------------------------

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")
        limit = min(int(args.get("limit", 5)), 20)

        result = self._mcp_client.call_tool(
            "mempalace_hybrid_search",
            {"query": query.strip()[:5000], "limit": limit},
            timeout=_TOOL_TIMEOUT,
        )

        if not result["success"]:
            return tool_error(result.get("error", "Search failed"))

        text = result.get("text", "").strip()
        if not text:
            return json.dumps({"result": "No relevant memories found."})

        if len(text) > 8000:
            text = text[:8000] + "\n\n[... truncated]"

        return json.dumps({"result": text})

    def _tool_save(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")
        room = args.get("room", "facts")

        result = self._mcp_client.call_tool(
            "mempalace_add_drawer",
            {"wing": "hermes", "room": room, "content": content},
            timeout=_TOOL_TIMEOUT,
        )

        if not result["success"]:
            return tool_error(result.get("error", "Save failed"))

        return json.dumps({"result": "Memory saved successfully."})

    def _tool_status(self) -> str:
        try:
            health = self._mcp_client.call_tool(
                "mempalace_health", {}, timeout=_TOOL_TIMEOUT,
            )
            if health.get("success"):
                status_text = health.get("text", "ok")
            else:
                status_text = "unavailable"
            return json.dumps({"status": f"memPalace: {status_text}"})
        except Exception as e:
            return tool_error(f"Status check failed: {e}")

    # -- Internal helpers ----------------------------------------------------

    def _palace_init_check(self) -> bool:
        """Check if the palace has been initialised (has .mempalace dir)."""
        marker = Path(self._palace_dir) / ".mempalace"
        return marker.is_dir()

    def _auto_init_palace(self) -> bool:
        """Initialise the palace with lightweight settings.

        Uses ``mpr init <dir> --no-llm --search-strategy contains --yes``.

        This is a one-time operation (subprocess), not MCP-based.
        """
        if not self._mpr_path:
            return False

        palace_dir = Path(self._palace_dir)
        palace_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = subprocess.run(
                [
                    self._mpr_path, "init", str(palace_dir),
                    "--no-llm",
                    "--search-strategy", "contains",
                    "--yes",
                ],
                capture_output=True, text=True,
                timeout=_INIT_TIMEOUT,
            )
            if result.returncode == 0:
                logger.info("MemPalace palace initialized at %s", palace_dir)
                return True
            else:
                logger.warning(
                    "mpr init failed (rc=%d): %s",
                    result.returncode, result.stderr.strip() or result.stdout.strip(),
                )
        except subprocess.TimeoutExpired:
            logger.warning("mpr init timed out after %ds", _INIT_TIMEOUT)
        except FileNotFoundError:
            logger.warning("mpr binary disappeared during auto-init")
        except Exception as e:
            logger.warning("mpr init error: %s", e)

        return False


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register MemPalace as a memory provider plugin."""
    ctx.register_memory_provider(MemPalaceMemoryProvider())
