"""Where detections and operator commands come from.

The camera path is not decided yet (browser/WebRTC like vision_follow, or straight from
mediad), so the app only depends on the small `DetectionSource` interface:

    source.latest() -> Optional[Frame]   # newest frame, or None if none yet
    source.live                           # True only for a real camera on this robot

`ReplaySource` plays a recorded JSONL file and is never live: the app refuses to move the
robot from replayed detections (see app.py). The live mediad source will be added later.

Commands are plain words so the keyboard backup and (later) English voice keywords share
one path: follow / gaze / stop / home / quit.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, TextIO

COMMANDS = ("follow", "gaze", "stop", "home", "greet", "quit",
            "sit", "stand", "crouch", "pick", "roll", "kick_left", "kick_right", "dance",
            "letsgo")  # voice "LET'S GO": FollowApp decides by mode (follow from idle, home on stage)

# Keyboard backup: one letter or word per line (works over plain SSH, no raw terminal needed).
KEY_ALIASES = {
    "f": "follow", "follow": "follow", "follow me": "follow",
    "g": "gaze", "gaze": "gaze", "look": "gaze",
    "s": "stop", "stop": "stop", "": "stop", " ": "stop",
    "h": "home", "home": "home", "let's go home": "home", "lets go home": "home",
    "d": "dance", "dance": "dance", "let's party": "dance", "shake it": "dance",
    "w": "greet", "hi": "greet", "hello": "greet", "say hello": "greet", "greet": "greet",
    # posture & skills (follow/posture.py); risky ones need the key twice within 3 s
    "x": "sit", "sit": "sit", "sit down": "sit",
    "u": "stand", "up": "stand", "stand": "stand", "stand up": "stand",
    "p": "crouch", "crouch": "crouch", "lie": "crouch", "lie down": "crouch", "down": "crouch",
    "1": "pick", "pick": "pick",
    "2": "roll", "roll": "roll",
    "3": "kick_left", "kick left": "kick_left",
    "4": "kick_right", "kick right": "kick_right",
    "q": "quit", "quit": "quit", "exit": "quit",
}


@dataclass(frozen=True)
class Frame:
    captured_at: float          # when the image was taken (same clock as the app)
    video_at: float             # when the newest decoded video frame arrived
    detections: Dict[str, Any]  # aruco_detector.detect() format: {width, height, boxes}


class NullSource:
    """No camera. The app can still gaze-hold and take commands, but never sees a target."""
    live = False

    def latest(self) -> Optional[Frame]:
        return None


class ReplaySource:
    """Replay JSONL lines {"t": seconds_from_start, "detections": {...}} on the app's clock."""
    live = False

    def __init__(self, records: Iterable[Dict[str, Any]], clock: Callable[[], float] = time.monotonic):
        self._records: List[Dict[str, Any]] = sorted(records, key=lambda r: float(r["t"]))
        self._clock = clock
        self._start: Optional[float] = None

    @classmethod
    def from_file(cls, path: str, clock: Callable[[], float] = time.monotonic) -> "ReplaySource":
        with open(path, encoding="utf-8") as handle:
            return cls((json.loads(line) for line in handle if line.strip()), clock)

    def latest(self) -> Optional[Frame]:
        now = self._clock()
        if self._start is None:
            self._start = now
        elapsed = now - self._start
        newest = None
        for record in self._records:
            if float(record["t"]) <= elapsed:
                newest = record
            else:
                break
        if newest is None:
            return None
        at = self._start + float(newest["t"])
        return Frame(captured_at=at, video_at=now, detections=newest["detections"])


class Recorder:
    """Write every new frame as a ReplaySource line, so a rehearsal can be replayed offline."""

    def __init__(self, path: str):
        self._handle = open(path, "w", encoding="utf-8")
        self._start: Optional[float] = None
        self._last = -float("inf")
        self.count = 0

    def write(self, frame: Optional[Frame]) -> None:
        if frame is None or frame.captured_at <= self._last or self._handle.closed:
            return
        self._last = frame.captured_at
        if self._start is None:
            self._start = frame.captured_at
        self._handle.write(json.dumps({"t": round(frame.captured_at - self._start, 4),
                                       "detections": frame.detections}, allow_nan=False) + "\n")
        self._handle.flush()
        self.count += 1

    def close(self) -> None:
        self._handle.close()


class ProcessDetectionSource:
    """Detections from a worker process (follow/vision/aruco_worker.py) over JSON lines.

    `live` is True only when the worker reads this robot's camera (`--device`); a worker fed
    from stdin or a file is never live, so the app will not drive the robot from it.
    """

    def __init__(self, *, live: bool, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        self.live = live
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._frame: Optional[Frame] = None
        self.ready = False
        self.error = ""
        self.frames = 0
        self.exposure: dict = {}
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._frame

    def handle_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("kind")
        if kind == "ready":
            self.ready = True
            self._log(f"相機就緒：{message.get('source')}（旋轉 {message.get('rotate')}°，{message.get('hz')} Hz）")
        elif kind == "error":
            self.error = str(message.get("message"))
            self._log(f"相機／偵測錯誤：{self.error}")
        elif kind == "warning":
            self._log(f"相機警告：{message.get('message')}")
        elif kind == "exposure":
            self.exposure = {k: message.get(k) for k in ("mean_luma", "exposure", "analogue_gain", "gain")}
            self._log("自動曝光：" + " ".join(f"{k}={v}" for k, v in self.exposure.items() if v is not None))
        elif kind == "frame":
            captured, detections = message.get("captured"), message.get("detections")
            if not isinstance(captured, (int, float)) or isinstance(captured, bool) or not isinstance(detections, dict):
                return
            now = self._clock()
            if captured > now + 0.05:  # a future capture time means the clocks disagree: drop it
                return
            with self._lock:
                self._frame = Frame(captured_at=float(captured), video_at=now, detections=detections)
            self.frames += 1

    def start(self, python: str, script: str, args: List[str]) -> "ProcessDetectionSource":
        self._process = subprocess.Popen([python, "-u", script, *args], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True)
        self._thread = threading.Thread(target=self._read, name="follow-camera", daemon=True)
        self._thread.start()
        return self

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self.handle_line(line)
        code = self._process.wait()
        if not self.error:
            self.error = f"偵測程式結束（{code}）"
        self._log(self.error)

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


def parse_command(text: str) -> Optional[str]:
    key = text.strip().lower()  # an empty line (just Enter) means stop
    if key not in KEY_ALIASES and len(key) > 1 and len(set(key)) == 1:
        key = key[0]  # "uu": a terminal (the desktop app's panel over SSH) doubled the keystroke
    return KEY_ALIASES.get(key)


class CommandQueue:
    """Thread-safe command inbox. Keyboard, voice and tests all `put()` into it."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[tuple]" = queue.Queue()

    def put(self, command: str, source: str = "keyboard") -> None:
        """`source` lets the app trust an operator's key more than a heard word."""
        if command not in COMMANDS:
            raise ValueError(f"unknown command {command!r}")
        self._queue.put((command, source))

    def drain_items(self) -> List[tuple]:
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items

    def drain(self) -> List[str]:
        return [command for command, _ in self.drain_items()]

    def read_lines(self, stream: Optional[TextIO] = None, log: Callable[[str], None] = print) -> threading.Thread:
        """Feed commands from a line-oriented stream on a daemon thread. EOF means quit.

        `stream` defaults to sys.stdin *at call time* (a default argument would freeze it at import).
        """
        stream = sys.stdin if stream is None else stream

        def reader() -> None:
            for line in stream:
                command = parse_command(line.rstrip("\n"))
                if command is None:
                    log(f"不認得的指令：{line.strip()!r}（f=跟隨 g=注視 w=打招呼 d=跳舞 h=回家 s/Enter=停 q=結束 x=坐下 u=站起來 p=趴下 1=撿 2=翻滾 3/4=踢）")
                else:
                    self.put(command)
            self.put("quit")

        thread = threading.Thread(target=reader, name="follow-keyboard", daemon=True)
        thread.start()
        return thread
