"""JSON-RPC 2.0 / NDJSON client for robotd's unix socket.

Wire format and behaviour follow `microduck/scripts/keyboard-control.py` (send/call) and the
v18 protocol in `duck-ipc-proto` (rev a9be46e), which is what the board's robotd speaks:

* notifications (no id) for continuous intents such as `robot.move`;
* requests (with id) wait for the matching response; `result.accepted == false` is a refusal;
* after `robot.subscribe` robotd pushes `{"method": "robot.state", "params": {...}}`.

A background reader thread owns the socket's receive side, so `call()` never has to drain
state pushes itself and the latest state is always available with its arrival time.

This module never decides whether motion is safe; that is `safety.py` and `motion.py`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

DEFAULT_SOCKET = "/run/robotd.sock"
# The board's robotd is a local build speaking v18 (robotctl there is v17). robotd answers
# `hello` with its own version and does not reject a mismatch, so this is informational.
CLIENT_API_VERSION = 18


class RobotdError(RuntimeError):
    """robotd returned a JSON-RPC error, or the connection failed."""


class Refused(RobotdError):
    """robotd answered `accepted: false`."""

    def __init__(self, method: str, reason: str):
        super().__init__(f"{method} refused: {reason}")
        self.method = method
        self.reason = reason


class Disconnected(RobotdError):
    """The socket closed or a send failed."""


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class RobotdClient:
    """Thread-safe robotd client over an already connected stream socket.

    Use `RobotdClient.connect()` on the board. Tests pass one end of `socket.socketpair()`.
    """

    def __init__(self, sock: socket.socket, *, clock: Callable[[], float] = time.monotonic):
        self._sock = sock
        self._sock.settimeout(None)
        self._clock = clock
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending: Dict[int, _Pending] = {}
        self._next_id = 0
        self._closed = threading.Event()
        self._error: Optional[BaseException] = None
        self.state: Optional[Dict[str, Any]] = None
        self.state_at: Optional[float] = None
        self.on_notification: Optional[Callable[[str, Any], None]] = None
        self._reader = threading.Thread(target=self._read_loop, name="robotd-reader", daemon=True)
        self._reader.start()

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def connect(cls, path: str = DEFAULT_SOCKET, timeout: float = 2.0) -> "RobotdClient":
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise RobotdError("this platform has no AF_UNIX; run on the robot or in WSL")
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except OSError as error:
            sock.close()
            raise Disconnected(f"cannot connect to {path}: {error}") from error
        return cls(sock)

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return not self._closed.is_set()

    def close(self) -> None:
        """Idempotent. Also called by the reader when robotd hangs up, so the fd never leaks."""
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()  # socket.close() is itself idempotent
        self._fail_pending()

    def __enter__(self) -> "RobotdClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── sending ──────────────────────────────────────────────────────────────
    def _write(self, message: Dict[str, Any]) -> None:
        if self._closed.is_set():
            raise Disconnected(f"robotd connection closed ({self._error or 'closed'})")
        data = (json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n").encode()
        try:
            with self._send_lock:
                self._sock.sendall(data)
        except OSError as error:
            self._error = error
            self.close()
            raise Disconnected(f"send failed: {error}") from error

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        """Fire-and-forget notification (continuous intents)."""
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, *,
             timeout: float = 3.0, allow_refusal: bool = False) -> Dict[str, Any]:
        """Request/response. Raises Refused on `accepted: false` unless allow_refusal."""
        with self._lock:
            self._next_id += 1
            ident = self._next_id
            pending = self._pending[ident] = _Pending()
        try:
            self._write({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
            if not pending.event.wait(timeout):
                raise RobotdError(f"timed out waiting {timeout:g}s for {method}")
        finally:
            with self._lock:
                self._pending.pop(ident, None)
        response = pending.response
        if response is None:
            raise Disconnected(f"connection lost while waiting for {method}")
        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {"message": response["error"]}
            raise RobotdError(f"{method} error {error.get('code')}: {error.get('message')}")
        result = response.get("result")
        if result is None:
            result = {}
        if isinstance(result, dict) and result.get("accepted") is False and not allow_refusal:
            raise Refused(method, str(result.get("reason") or "no reason given"))
        return result

    # ── convenience (read-only and stop only; motion lives in motion.py) ─────
    def hello(self) -> Dict[str, Any]:
        return self.call("hello", {"api_version": CLIENT_API_VERSION})

    def health(self, timeout: float = 1.0) -> Dict[str, Any]:
        return self.call("robot.health", {}, timeout=timeout)

    def mode(self, timeout: float = 1.0) -> str:
        result = self.call("robot.mode", {}, timeout=timeout)
        if not isinstance(result, dict):
            raise RobotdError(f"robot.mode 回覆格式不對：{type(result).__name__}")
        return str(result.get("mode", ""))

    def subscribe(self, hz: int = 10) -> Dict[str, Any]:
        return self.call("robot.subscribe", {"hz": int(hz)})

    def stop(self, timeout: float = 1.0) -> bool:
        """Best-effort `robot.stop`. Never raises; returns True when robotd acknowledged."""
        try:
            result = self.call("robot.stop", {}, timeout=timeout, allow_refusal=True)
        except Exception:  # noqa: BLE001 - "never raises" is the contract the stop paths rely on
            return False
        return not (isinstance(result, dict) and result.get("accepted") is False)

    def state_age(self) -> Optional[float]:
        return None if self.state_at is None else self._clock() - self.state_at

    # ── receiving ────────────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        buffer = b""
        try:
            while not self._closed.is_set():
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise Disconnected("robotd closed the connection")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._dispatch(line)
        except (OSError, RobotdError) as error:
            if not self._closed.is_set():
                self._error = error
        finally:
            self.close()

    def _dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return  # robotd only writes JSON; ignore a corrupt line rather than die
        if not isinstance(message, dict):
            return
        ident = message.get("id")
        if ident is not None and ("result" in message or "error" in message):
            with self._lock:
                pending = self._pending.get(ident)
            if pending is not None:
                pending.response = message
                pending.event.set()
            return
        method = message.get("method")
        if method == "robot.state" and isinstance(message.get("params"), dict):
            self.state = message["params"]
            self.state_at = self._clock()
        if method and self.on_notification is not None:
            try:
                self.on_notification(method, message.get("params"))
            except Exception:  # a buggy observer must not kill the reader
                pass

    def _fail_pending(self) -> None:
        with self._lock:
            waiting = list(self._pending.values())
        for pending in waiting:
            pending.event.set()
