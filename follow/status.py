"""One-line operator status, repainted in place, with event lines printed above it.

    [follow·moving] 短程前進，到時停步 | 8.12V 61°C | 相機 0.4s | 語音正常 · 上一句 follow

On a terminal the line is redrawn at most `period` seconds apart with \\r + clear-line, and
every log message first clears it, prints on its own line, then redraws — so events never get
mixed into the status. Piped or redirected (not a TTY) it prints the status as a normal line
every `pipe_period` seconds instead, so `| tee show.log` stays readable.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Optional, TextIO

CLEAR = "\r\033[K"


def compose(app: Any, now: float, voice: Any = None, camera: Any = None, heard: Optional[str] = None) -> str:
    """Build the status text from the app, its guard, and optional voice/camera sources."""
    guard = app.guard
    decision = app.last_decision
    phase = getattr(decision, "phase", None) if decision is not None else None
    head = f"[{app.mode}{'·' + phase if phase else ''}]"
    if app.mode == "idle":
        what = "待命"
    elif app.problem:
        what = "不可移動：" + app.problem
    else:
        what = getattr(decision, "reason", "") or ""
    health = guard.health or {}
    volts = (health.get("battery") or {}).get("volts")
    cpu = health.get("cpu_temp_c")
    power = f"{volts:.2f}V" if isinstance(volts, (int, float)) else "?V"
    power += f" {cpu:.0f}°C" if isinstance(cpu, (int, float)) else ""
    if guard.battery_low:
        power += " ⚠請充電"
    parts = [f"{head} {what}", power]
    if guard.dry_run:
        parts.append("DRY")
    if camera is not None:
        frame = camera.latest()
        if getattr(camera, "error", ""):
            parts.append("相機✗")
        elif frame is None:
            parts.append("相機等待中")
        else:
            parts.append(f"相機 {max(0.0, now - frame.video_at):.1f}s")
    if voice is not None:
        text = voice.status()
        if heard:
            text += f" · 上一句 {heard}"
        parts.append(text)
    return " | ".join(parts)


class StatusPrinter:
    def __init__(self, stream: TextIO = None, *, period: float = 0.5, pipe_period: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.stream = stream or sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.period = period if self.tty else pipe_period
        self._clock = clock
        self._lock = threading.Lock()
        self._line = ""
        self._shown_at = -1e18

    def log(self, message: str) -> None:
        """Print an event on its own line without disturbing the status line."""
        with self._lock:
            if self.tty and self._line:
                self.stream.write(CLEAR + message + "\n" + self._fit(self._line))
            else:
                self.stream.write(message + "\n")
            self.stream.flush()

    def update(self, line: str) -> None:
        """TTY: redraw when the text changes, or every `period` anyway (ages tick up).
        Pipe: print one line every `pipe_period`, whatever changed in between."""
        now = self._clock()
        with self._lock:
            due = now - self._shown_at >= self.period
            if self.tty:
                if line == self._line and not due:
                    return
            elif not due:
                self._line = line
                return
            self._line = line
            self._shown_at = now
            self.stream.write((CLEAR + self._fit(line)) if self.tty else (line + "\n"))
            self.stream.flush()

    def finish(self) -> None:
        with self._lock:
            if self.tty and self._line:
                self.stream.write("\n")
                self.stream.flush()

    @staticmethod
    def _fit(line: str, columns: Optional[int] = None) -> str:
        """Cut to the terminal width, counting CJK characters as two columns."""
        import shutil
        import unicodedata
        width = (columns or shutil.get_terminal_size((100, 20)).columns) - 1
        out, used = [], 0
        for char in line:
            used += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if used > width:
                break
            out.append(char)
        return "".join(out)
