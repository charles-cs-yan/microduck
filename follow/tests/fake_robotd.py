"""In-process stand-in for robotd over socket.socketpair(). Test-only."""

from __future__ import annotations

import copy
import json
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

from follow.robotd_client import RobotdClient


def healthy_health(**overrides: Any) -> Dict[str, Any]:
    health = {
        "healthy": True,
        "battery": {"volts": 8.2, "percent": 90.0},
        "motors": {"hottest": "head_yaw", "max_c": 34.0, "mean_c": 30.0},
        "cpu_temp_c": 59.0,
        "control_loop": {"target_hz": 50.0, "achieved_hz": 50.0, "ticks": 1, "missed": 0, "last_tick_age_ms": 4},
        "bus": {"consecutive_errors": 0, "startup_failures": 0},
        "imu": {"ready": True, "stale_blocks": 0, "consecutive_stale_blocks": 0},
    }
    health.update(overrides)
    return health


def standing_state(**overrides: Any) -> Dict[str, Any]:
    state = {
        "t": 1.0,
        "move": {"requested": [0, 0, 0], "applied": [0, 0, 0]},
        "head": [0.0, 0.0, 0.0, 0.0],
        "policy": "stand",
        "safety": {"fallen": False, "limp": False, "gravity": [0, 0, -1]},
        "loop": {},
        "joints": [],
        "targets": [],
    }
    state.update(overrides)
    return state


class FakeRobotd:
    """Answers requests with `handlers[method](params)`; records everything received."""

    def __init__(self) -> None:
        self.server, client_sock = socket.socketpair()
        self.received: List[Dict[str, Any]] = []
        self.health = healthy_health()
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "hello": lambda p: {"api_version": 18},
            "robot.health": lambda p: copy.deepcopy(self.health),
            "robot.subscribe": lambda p: {"hz": p.get("hz")},
            "robot.mode": lambda p: {"mode": "walk"},
            "robot.stop": lambda p: {"accepted": True},
            "robot.move": lambda p: {"accepted": True},
            "robot.sound": lambda p: {"accepted": True},
            "robot.do": lambda p: {"accepted": True},
            "robot.enable": lambda p: {"accepted": True, "reason": "enabled — driving"},
        }
        self.silent: set = set()  # methods to never answer (timeouts)
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.client = RobotdClient(client_sock)

    def methods(self) -> List[str]:
        with self._lock:
            return [m.get("method") for m in self.received]

    def moves(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [m["params"] for m in self.received if m.get("method") == "robot.move"]

    def push(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def push_raw(self, data: bytes) -> None:
        self.server.sendall(data)

    def hangup(self) -> None:
        """Simulate robotd going away. shutdown() first: close() alone does not wake a recv()."""
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()

    def close(self) -> None:
        self.client.close()
        try:
            self.server.close()
        except OSError:
            pass

    def _send(self, message: Dict[str, Any]) -> None:
        self.server.sendall((json.dumps(message) + "\n").encode())

    def _serve(self) -> None:
        buffer = b""
        try:
            while True:
                chunk = self.server.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    with self._lock:
                        self.received.append(message)
                    if "id" not in message or message["method"] in self.silent:
                        continue
                    handler: Optional[Callable] = self.handlers.get(message["method"])
                    if handler is None:
                        self._send({"jsonrpc": "2.0", "id": message["id"],
                                    "error": {"code": -32601, "message": "method not found"}})
                    else:
                        self._send({"jsonrpc": "2.0", "id": message["id"], "result": handler(message.get("params", {}))})
        except OSError:
            return
