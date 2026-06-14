"""
WebSocket terminal endpoint — VS Code-style persistent PTY sessions.

Key design: PTY process lifetime is DECOUPLED from WebSocket lifetime.
- WS disconnect → process keeps running (like VS Code)
- WS reconnect → attach to existing process + replay output buffer
- Process exit → session cleaned up automatically
- Idle sessions cleaned up after TTL

Messages:
  Client → Server: {"type": "input", "data": "..."} | {"type": "resize", "cols": N, "rows": N}
  Server → Client: {"type": "output", "data": "..."} | {"type": "exit"}
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, Set

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = structlog.get_logger(__name__)

terminal_router = APIRouter()

_CONNECTORS_ROOT = Path(__file__).resolve().parent.parent.parent
_GENERATED_ROOT  = _CONNECTORS_ROOT / "generated_connectors"

# How long (seconds) to keep an idle session alive after all clients disconnect
SESSION_TTL_IDLE = 2 * 60 * 60  # 2 hours
# Max output buffer per session
OUTPUT_BUFFER_BYTES = 512 * 1024  # 512 KB


# ─── Persistent session registry ─────────────────────────────────────────────

class TerminalSession:
    """
    A live PTY process that persists across WebSocket reconnects.
    Multiple WebSocket clients can subscribe simultaneously (e.g. multiple tabs).
    """

    def __init__(self, session_id: str, proc, loop: asyncio.AbstractEventLoop):
        self.session_id  = session_id
        self.proc        = proc           # ptyprocess.PtyProcessUnicode
        self.loop        = loop
        self.created_at  = time.time()
        self.last_active = time.time()

        # Rolling output buffer for replay on reconnect
        self._buf_lock  = threading.Lock()
        self._buf: deque[str] = deque()
        self._buf_size  = 0

        # Active WebSocket subscribers
        self._sub_lock: threading.Lock = threading.Lock()
        self._subscribers: Set[WebSocket] = set()

        # Reader thread
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ── Buffer ────────────────────────────────────────────────────────────────

    def _buf_append(self, data: str) -> None:
        with self._buf_lock:
            self._buf.append(data)
            self._buf_size += len(data)
            # Trim oldest chunks when over limit
            while self._buf_size > OUTPUT_BUFFER_BYTES and self._buf:
                removed = self._buf.popleft()
                self._buf_size -= len(removed)

    def get_replay(self) -> str:
        with self._buf_lock:
            return "".join(self._buf)

    # ── Subscribers ───────────────────────────────────────────────────────────

    def subscribe(self, ws: WebSocket) -> None:
        with self._sub_lock:
            self._subscribers.add(ws)
        self.last_active = time.time()

    def unsubscribe(self, ws: WebSocket) -> None:
        with self._sub_lock:
            self._subscribers.discard(ws)
        self.last_active = time.time()

    def _broadcast(self, msg: str) -> None:
        with self._sub_lock:
            subs = set(self._subscribers)
        for ws in subs:
            try:
                asyncio.run_coroutine_threadsafe(
                    ws.send_text(msg), self.loop
                ).result(timeout=3)
            except Exception:
                pass  # subscriber disconnected — will be removed on WS close

    # ── PTY reader thread ─────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.proc.read(4096)
                self.last_active = time.time()
                self._buf_append(data)
                self._broadcast(json.dumps({"type": "output", "data": data}))
            except EOFError:
                # Process exited normally
                exit_msg = json.dumps({"type": "exit"})
                self._broadcast(exit_msg)
                _sessions.remove(self.session_id)
                break
            except Exception:
                break

    # ── Write to PTY ──────────────────────────────────────────────────────────

    def write(self, data: str) -> None:
        if self.proc.isalive():
            self.proc.write(data)
            self.last_active = time.time()

    def resize(self, rows: int, cols: int) -> None:
        if self.proc.isalive():
            try:
                self.proc.setwinsize(rows, cols)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc.isalive()

    def terminate(self) -> None:
        self._stop.set()
        try:
            if self.proc.isalive():
                self.proc.terminate(force=True)
        except Exception:
            pass


class _SessionRegistry:
    """Thread-safe global registry of active terminal sessions."""

    def __init__(self):
        self._lock: threading.Lock = threading.Lock()
        self._store: dict[str, TerminalSession] = {}

    def get(self, session_id: str) -> Optional[TerminalSession]:
        with self._lock:
            return self._store.get(session_id)

    def put(self, session_id: str, session: TerminalSession) -> None:
        with self._lock:
            self._store[session_id] = session

    def remove(self, session_id: str) -> None:
        with self._lock:
            sess = self._store.pop(session_id, None)
        if sess:
            sess.terminate()
            logger.info("terminal.session_removed", session_id=session_id)

    def reap_idle(self) -> None:
        """Remove sessions that have been idle beyond TTL."""
        now = time.time()
        with self._lock:
            stale = [
                sid for sid, s in self._store.items()
                if (now - s.last_active) > SESSION_TTL_IDLE
            ]
        for sid in stale:
            logger.info("terminal.session_ttl_expired", session_id=sid)
            self.remove(sid)


_sessions = _SessionRegistry()


# ── Background reaper ─────────────────────────────────────────────────────────

async def _reaper_loop():
    while True:
        await asyncio.sleep(15 * 60)  # check every 15 min
        _sessions.reap_idle()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_connector_dir(session_id: str, tenant_id: str, provider: str, service: str) -> Path:
    if tenant_id and provider and service:
        service_slug = service.lower().replace("-", "_").replace(" ", "_")
        for name in (f"{service_slug}_connector", service_slug):
            candidate = _GENERATED_ROOT / tenant_id / name
            if candidate.exists():
                return candidate

    if _GENERATED_ROOT.exists():
        for tenant_dir in _GENERATED_ROOT.iterdir():
            if not tenant_dir.is_dir():
                continue
            for conn_dir in tenant_dir.iterdir():
                if conn_dir.is_dir() and service.lower().replace("-", "_") in conn_dir.name.lower():
                    return conn_dir

    return _GENERATED_ROOT if _GENERATED_ROOT.exists() else Path.home()


def _spawn_pty(cwd: Path) -> "ptyprocess.PtyProcessUnicode":
    import ptyprocess  # type: ignore
    env = dict(os.environ)
    env.update({
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "LANG": "en_US.UTF-8",
        # Suppress macOS "default shell is now zsh" banner
        "BASH_SILENCE_DEPRECATION_WARNING": "1",
    })
    # Use the user's own shell (zsh on macOS Catalina+, bash elsewhere).
    # No extra flags — ptyprocess opens a real PTY so the shell detects
    # interactivity automatically. Avoids incompatible flag combos on bash 3.2.
    shell = env.get("SHELL", "/bin/bash")
    return ptyprocess.PtyProcessUnicode.spawn(
        [shell],
        cwd=str(cwd),
        env=env,
        dimensions=(50, 220),
    )


# ─── WebSocket endpoint ───────────────────────────────────────────────────────

@terminal_router.websocket("/integration/api/v1/terminal/{session_id}")
async def terminal_ws(
    websocket: WebSocket,
    session_id: str,
    tenant_id: str = Query(default=""),
    provider:   str = Query(default=""),
    service:    str = Query(default=""),
):
    await websocket.accept()
    loop = asyncio.get_event_loop()

    # Ensure reaper is running
    try:
        asyncio.ensure_future(_reaper_loop())
    except RuntimeError:
        pass

    # ── Try to attach to existing session ────────────────────────────────────
    sess = _sessions.get(session_id)

    if sess and sess.is_alive():
        logger.info("terminal.reconnect", session_id=session_id)
        sess.subscribe(websocket)

        # Replay buffered output so client sees what it missed
        replay = sess.get_replay()
        if replay:
            reconnect_banner = (
                f"\r\n\x1b[33m--- Reconnected: {time.strftime('%H:%M:%S')} ---\x1b[0m\r\n"
            )
            try:
                await websocket.send_text(json.dumps({"type": "output", "data": reconnect_banner + replay}))
            except Exception:
                pass
    else:
        # ── Spawn a new PTY session ───────────────────────────────────────────
        if sess:
            _sessions.remove(session_id)  # clean up dead session

        try:
            import ptyprocess  # type: ignore
        except ImportError:
            await websocket.send_text(json.dumps({
                "type": "output",
                "data": "\r\n\x1b[31mError: ptyprocess not installed. Run: pip install ptyprocess\x1b[0m\r\n",
            }))
            await websocket.close()
            return

        cwd = _get_connector_dir(session_id, tenant_id, provider, service)
        logger.info("terminal.new_session", session_id=session_id, cwd=str(cwd))

        proc = _spawn_pty(cwd)
        sess = TerminalSession(session_id, proc, loop)
        _sessions.put(session_id, sess)
        sess.subscribe(websocket)

        welcome = (
            f"\r\n\x1b[32m# Shielva Integration Terminal\x1b[0m\r\n"
            f"\x1b[90m# Session: {session_id}\x1b[0m\r\n"
            f"\x1b[90m# Directory: {cwd}\x1b[0m\r\n"
            f"\x1b[90m# Session persists across reconnects\x1b[0m\r\n\r\n"
        )
        sess._buf_append(welcome)
        try:
            await websocket.send_text(json.dumps({"type": "output", "data": welcome}))
        except Exception:
            pass

    # ── Handle incoming messages from this client ─────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg["type"] == "input":
                sess.write(msg["data"])
            elif msg["type"] == "resize":
                sess.resize(int(msg.get("rows", 50)), int(msg.get("cols", 220)))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("terminal.ws_error", session_id=session_id, error=str(exc))
    finally:
        # ── CRITICAL: only unsubscribe, NEVER kill the process ───────────────
        sess.unsubscribe(websocket)
        logger.info("terminal.client_disconnected", session_id=session_id,
                    proc_alive=sess.is_alive())
