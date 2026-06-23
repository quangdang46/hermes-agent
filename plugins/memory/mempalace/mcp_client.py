"""MemPalace MCP client — persistent JSON-RPC stdio connection to ``mpr serve``.

Spawns ``mpr serve`` as a subprocess and speaks raw JSON-RPC 2.0 over
stdin/stdout (line-delimited JSON). A background daemon thread runs the
asyncio event loop; synchronous ``call_tool()`` bridges into it via
``concurrent.futures.Future`` (thread-safe, no loop binding issues).

Zero external dependencies — no ``mcp`` PyPI package required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import concurrent.futures
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _encode_request(method: str, params: dict, msg_id: int) -> bytes:
    """Build a JSON-RPC 2.0 request as a single line ending with ``\\n``."""
    return json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": msg_id,
    }).encode() + b"\n"


def _decode_line(line: bytes) -> Optional[dict]:
    """Parse a single JSON-RPC response/notification line."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("MCP non-JSON: %.200s", stripped)
        return None


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

class MemPalaceMCPClient:
    """Persistent MCP stdio client for mempalace.

    Spawns ``mpr serve`` and communicates via raw JSON-RPC 2.0 over
    stdin/stdout. A background daemon thread holds the asyncio event loop.
    """

    def __init__(self, mpr_path: str, palace_dir: str):
        self._mpr_path = mpr_path
        self._palace_dir = palace_dir

        # Subprocess
        self._process: Optional[subprocess.Popen] = None

        # Async components (owned by background thread)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_reader: Optional[asyncio.StreamReader] = None
        self._async_writer: Optional[asyncio.StreamWriter] = None

        # Pending calls — thread-safe concurrent.futures.Future (not asyncio!)
        self._lock = threading.Lock()
        self._msg_id = 0
        self._pending: Dict[int, concurrent.futures.Future] = {}

        # Thread lifecycle
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- Public API ----------------------------------------------------------

    def start(self, timeout: float = 15.0) -> bool:
        """Spawn ``mpr serve`` and perform the MCP initialize handshake.

        Returns True if the session is ready within *timeout* seconds.
        """
        if self._process is not None:
            return True

        # Kill any stale mpr process
        self._cleanup_stale()

        try:
            self._process = subprocess.Popen(
                [self._mpr_path, "serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._palace_dir,
                env={**os.environ, "MPR_PALACE_DIR": self._palace_dir},
            )
        except FileNotFoundError:
            logger.error("mpr binary not found at %s", self._mpr_path)
            return False
        except Exception as e:
            logger.error("Failed to spawn mpr serve: %s", e)
            return False

        # Start background event-loop thread
        self._stopped.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="mcp-event-loop",
        )
        self._thread.start()

        # Wait for MCP session ready
        if not self._ready.wait(timeout=timeout):
            logger.warning("MCP not ready within %.1fs", timeout)
            self.stop()
            return False

        return True

    def is_connected(self) -> bool:
        """Check if the MCP session appears alive."""
        if self._process is None or self._process.poll() is not None:
            return False
        if self._loop is None or self._async_writer is None:
            return False
        return True

    def call_tool(self, name: str, args: dict, timeout: float = 30.0) -> dict:
        """Call an MCP tool synchronously.

        Returns ``{"success": bool, "text": str, "error": str}``.
        """
        if not self.is_connected():
            return {"success": False, "error": "MCP not connected", "text": ""}

        future: concurrent.futures.Future = concurrent.futures.Future()

        with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id
            self._pending[msg_id] = future
            request = _encode_request("tools/call", {
                "name": name,
                "arguments": args,
            }, msg_id)

            if self._async_writer is None:
                self._pending.pop(msg_id, None)
                return {"success": False, "error": "MCP writer unavailable", "text": ""}

            # Schedule write on the event loop thread
            async def _do_write():
                self._async_writer.write(request)
                await self._async_writer.drain()

            try:
                asyncio.run_coroutine_threadsafe(
                    _do_write(), self._loop
                )
            except Exception as e:
                self._pending.pop(msg_id, None)
                return {"success": False, "error": f"Write failed: {e}", "text": ""}

        # Wait for result (from background thread reader)
        try:
            result = future.result(timeout=timeout)
            return self._format_result(result)
        except concurrent.futures.TimeoutError:
            with self._lock:
                self._pending.pop(msg_id, None)
            return {"success": False, "error": f"Tool call timed out ({timeout}s)", "text": ""}
        except Exception as e:
            return {"success": False, "error": str(e), "text": ""}

    def stop(self) -> None:
        """Graceful shutdown: stop loop, kill subprocess."""
        self._stopped.set()

        # Cancel pending futures
        with self._lock:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("MCP client shutting down"))
            self._pending.clear()

        # Kill subprocess
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=2)
                except Exception:
                    pass
        self._process = None
        self._async_writer = None
        self._async_reader = None

    # -- Background thread ---------------------------------------------------

    def _run_loop(self) -> None:
        """Daemon thread entry — runs the asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        try:
            loop.run_until_complete(self._run_session())
        except Exception as e:
            logger.debug("MCP event loop exited: %s", e)
        finally:
            loop.close()
            self._loop = None

    async def _run_session(self) -> None:
        """Full MCP session lifecycle (connect → init → operation)."""
        try:
            self._async_reader, self._async_writer = await self._connect_pipes()
            await self._handshake()
            self._ready.set()
            await self._reader_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._stopped.is_set():
                logger.error("MCP session error: %s", e)
        finally:
            self._ready.clear()

    async def _connect_pipes(self):
        """Wrap subprocess pipes in asyncio streams."""
        if self._process is None or self._process.stdout is None or self._process.stdin is None:
            raise RuntimeError("Subprocess not running")

        loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(
            lambda: protocol, self._process.stdout,
        )

        write_transport, write_protocol = await loop.connect_write_pipe(
            lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()),
            self._process.stdin,
        )
        writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)

        return reader, writer

    async def _handshake(self) -> None:
        """Perform the MCP initialize handshake.

        1. Send ``initialize`` request
        2. Wait for ``initialize`` result
        3. Send ``notifications/initialized``
        """
        writer = self._async_writer
        reader = self._async_reader
        if writer is None or reader is None:
            raise RuntimeError("Pipes not connected")

        # 1. Send initialize request
        init_req = _encode_request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "hermes-mempalace", "version": "0.8.0"},
        }, 1)
        writer.write(init_req)
        await writer.drain()

        # 2. Read until we get a valid initialize result
        seen_stderr = []
        for _ in range(50):  # up to 50 lines to account for startup messages
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not line:
                break
            decoded = line.decode().strip()
            if not decoded:
                continue
            msg = _decode_line(line)
            if msg is not None and msg.get("id") == 1 and "result" in msg:
                server_info = msg["result"].get("serverInfo", {})
                logger.info(
                    "MCP initialized: %s v%s",
                    server_info.get("name", "?"),
                    server_info.get("version", "?"),
                )
                break
            # May be stderr output before JSON starts
            if decoded.startswith("{"):
                logger.debug("MCP unhandled init line: %.100s", decoded)
            else:
                seen_stderr.append(decoded)
        else:
            logger.warning(
                "MCP initialize: no valid response (stderr: %s)",
                " | ".join(seen_stderr[-5:]),
            )
            return

        # 3. Send initialized notification (no id — notification)
        notif = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }).encode() + b"\n"
        writer.write(notif)
        await writer.drain()

    async def _reader_loop(self) -> None:
        """Read JSON-RPC responses and resolve pending futures.

        Returns when the connection drops.
        """
        reader = self._async_reader
        if reader is None:
            return

        while not self._stopped.is_set():
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            except asyncio.TimeoutError:
                # Periodic ping to check liveness
                if self._async_writer:
                    try:
                        ping_req = json.dumps({
                            "jsonrpc": "2.0", "method": "ping",
                            "params": {}, "id": -1,
                        }).encode() + b"\n"
                        self._async_writer.write(ping_req)
                        await self._async_writer.drain()
                    except Exception:
                        pass
                continue

            if not line:
                break  # EOF = server died

            msg = _decode_line(line)
            if msg is None:
                continue

            msg_id = msg.get("id")
            if msg_id is not None:
                with self._lock:
                    future = self._pending.pop(msg_id, None)
                if future is not None and not future.done():
                    future.set_result(msg)

    # -- Cleanup -------------------------------------------------------------

    def _cleanup_stale(self) -> None:
        """Terminate any existing mpr serve process."""
        import signal
        try:
            subprocess.run(
                ["pkill", "-f", "mpr serve"],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    # -- Formatting ----------------------------------------------------------

    @staticmethod
    def _format_result(msg: dict) -> dict:
        """Convert an MCP tool call response into ``{success, text, error}``."""
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            return {
                "success": False,
                "error": err.get("message", str(err)),
                "text": "",
            }

        result = msg.get("result", {})
        if isinstance(result, dict):
            content = result.get("content", [])
            is_error = result.get("isError", False)

            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") == "resource":
                        parts.append(
                            json.dumps(item.get("resource", ""), ensure_ascii=False)
                        )
                elif isinstance(item, str):
                    parts.append(item)

            text = "\n".join(parts).strip()
            if is_error:
                return {"success": False, "error": text, "text": text}
            return {"success": True, "text": text, "error": ""}

        text = str(result).strip()
        return {"success": True, "text": text, "error": ""}
