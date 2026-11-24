"""English voice commands → CommandQueue. Standard library only.

Starts `follow/voice/kws_listen.py` with the speech environment's Python and reads its JSON
lines. Recognised labels map onto the same commands as the keyboard backup:

    FOLLOW ME → follow · LET'S GO HOME / GO HOME → home · STOP → stop · LOOK AT ME → gaze

Voice only produces commands; every motion still goes through MotionGuard. Safety notes:
* "stop" is never rate-limited; other commands have a per-command cooldown so one utterance
  decoded twice does not restart a mode.
* If the listener dies or its audio heartbeat goes silent, voice is reported as down; the
  robot keeps doing what it was told (stopping is not voice's job — MotionGuard's gates and
  the operator's keyboard remain). The status is shown so the operator can take over.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .sources import CommandQueue

HERE = Path(__file__).resolve().parent
LISTENER = HERE / "voice" / "kws_listen.py"
LABELS = {"follow": "follow", "home": "home", "stop": "stop", "gaze": "gaze", "greet": "greet",
          "sit": "sit", "stand": "stand", "crouch": "crouch", "dance": "dance",
          "letsgo": "letsgo"}


class VoiceCommands:
    def __init__(self, commands: CommandQueue, *, cooldown: float = 2.0, heartbeat_timeout: float = 1.0,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        self.commands = commands
        self.cooldown = cooldown
        self.heartbeat_timeout = heartbeat_timeout
        self._clock = clock
        self._log = log
        self._last: Dict[str, float] = {}
        self.heartbeat_at: Optional[float] = None
        self.ready = False
        self.error = ""
        self.heard: List[str] = []
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    # ── parsing (also used directly by tests) ───────────────────────────────
    def handle_line(self, line: str) -> Optional[str]:
        """Process one listener line; returns the command queued, if any."""
        try:
            message = json.loads(line)
        except ValueError:
            return None
        if not isinstance(message, dict):
            return None
        kind = message.get("kind")
        now = self._clock()
        if kind == "heartbeat":
            self.heartbeat_at = now
        elif kind == "ready":
            self.ready = True
            self.heartbeat_at = now
            self._log(f"語音就緒（{message.get('model')}）")
        elif kind == "error":
            self.error = str(message.get("message"))
            self._log(f"語音錯誤：{self.error}")
        elif kind == "keyword":
            label = str(message.get("label", "")).strip().lower()
            command = LABELS.get(label)
            if command is None:
                self._log(f"語音：不認得的標籤 {label!r}")
                return None
            if command != "stop" and now - self._last.get(command, -1e9) < self.cooldown:
                return None
            self._last[command] = now
            self.heard.append(command)
            self._log(f"語音：{label} → {command}")
            self.commands.put(command, source="voice")
            return command
        return None

    def status(self) -> str:
        if self.error:
            return f"語音中斷：{self.error}"
        if self._process is not None and self._process.poll() is not None:
            return f"語音程式已結束（{self._process.returncode}）"
        if not self.ready:
            return "語音載入中"
        if self.heartbeat_at is None or self._clock() - self.heartbeat_at > self.heartbeat_timeout:
            return "語音沒有音訊心跳"
        return "語音正常"

    # ── subprocess ───────────────────────────────────────────────────────────
    def start(self, python: str, args: Optional[List[str]] = None, script: Path = LISTENER) -> "VoiceCommands":
        self._process = subprocess.Popen([python, "-u", str(script), *(args or [])],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._thread = threading.Thread(target=self._read, name="follow-voice", daemon=True)
        self._thread.start()
        return self

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self.handle_line(line)
        code = self._process.wait()
        if not self.error:
            self.error = f"語音程式結束（{code}）"
        self._log(self.status())

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()


def default_python(speech_dir: Path) -> str:
    """The voice venv made by voice/setup_venv.sh, else upstream speech's venv."""
    import os
    exe = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    # Not relative to this file: deployed code lives in ~/microduck-follow/releases/<stamp>/follow/.
    base = Path(os.environ.get("FOLLOW_HOME", Path.home() / "microduck-follow"))
    own = base / "venv-voice" / exe
    return str(own if own.is_file() else speech_dir / ".venv" / exe)
