"""Software auto-exposure for when mediad is stopped. Port of mediad/src/exposure.rs (rev 04b1c00).

Why it exists: Rockchip's rkaiq 3A sets the exposure once at stream start and then stops, and
mediad keeps it right with this loop. With mediad stopped (follow reads the camera itself) the
loop has to run here, or the picture keeps whatever exposure it had when the stream started —
a problem under stage lights.

Same constants and the same maths as mediad: mean luma (after the ISP gamma) towards 90 with a
12 % deadband; a damped multiplicative step (ratio^0.6) on the product exposure × analogue ×
digital, spent in noise order — shutter to 600 lines, analogue gain to 11×, shutter to the hard
1200-line cap (longer stretches the frame time), digital gain to 16× last. Writes go through
`v4l2-ctl` on the capture node, sensor controls and ISP gain in separate calls, and are
re-asserted every 10 s even when nothing changed.

Pure except for `V4l2Writer`, which tests replace.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

TARGET_Y = 90.0
DEADBAND = 0.12
SOFT_LINES = 600.0
HARD_LINES = 1200.0
MAX_ANALOGUE = 11.0
MAX_DIGITAL = 16.0
INTERVAL = 0.5
REASSERT_TICKS = 20


@dataclass(frozen=True)
class Controls:
    exposure: int        # sensor lines
    analogue_gain: int   # 256 = 1×
    gain: int            # ISP digital gain, 256 = 1×


class Ae:
    def __init__(self, exposure_lines: float, analogue_gain_reg: float):
        self.exposure = min(max(float(exposure_lines), 4.0), HARD_LINES)
        self.analogue = min(max(float(analogue_gain_reg) / 256.0, 1.0), MAX_ANALOGUE)
        self.digital = 1.0
        self.written: Optional[Controls] = None

    def controls(self) -> Controls:
        return Controls(int(self.exposure), int(self.analogue * 256.0), int(self.digital * 256.0))

    def step(self, mean_luma: float) -> Optional[Controls]:
        """One step towards the setpoint; None inside the deadband or when nothing would change."""
        ratio = min(max(TARGET_Y / max(mean_luma, 1.0), 0.25), 4.0)
        if abs(1.0 - ratio) < DEADBAND:
            return None
        budget = min(max(self.exposure * self.analogue * self.digital * ratio ** 0.6, 4.0),
                     HARD_LINES * MAX_ANALOGUE * MAX_DIGITAL)
        self.exposure = min(budget, SOFT_LINES)
        self.analogue = min(max(budget / self.exposure, 1.0), MAX_ANALOGUE)
        self.exposure = min(max(budget / self.analogue, self.exposure), HARD_LINES)
        self.digital = min(max(budget / (self.exposure * self.analogue), 1.0), MAX_DIGITAL)
        nxt = self.controls()
        if self.written == nxt:
            return None
        self.written = nxt
        return nxt

    def current(self) -> Controls:
        controls = self.controls()
        self.written = controls
        return controls


class V4l2Writer:
    """Writes controls with v4l2-ctl, like mediad (two calls: sensor, then ISP gain)."""

    TIMEOUT = 1.0  # s; this runs inside the frame loop, a hung v4l2-ctl must not stall it

    def __init__(self, device: str, run: Callable = subprocess.run):
        self.device = device
        self._run = run
        self.gain_unsupported = False

    def _call(self, args):
        try:
            return self._run(["v4l2-ctl", "-d", self.device, *args], capture_output=True, text=True,
                             timeout=self.TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise OSError(f"v4l2-ctl {' '.join(args)} 超過 {self.TIMEOUT:g} 秒") from error

    def _set(self, controls: str) -> None:
        result = self._call([f"--set-ctrl={controls}"])
        if result.returncode != 0:
            raise OSError(f"{controls}: {result.stderr.strip()}")

    def write(self, c: Controls, force_gain: bool = False) -> None:
        """Sensor controls must land. ISP gain is written when above 1×, or when forced (first
        write / re-assert) so a high digital gain left behind by mediad gets reset to 1× — mediad
        itself never writes 1× back. A node without `gain` only matters when we need > 1×."""
        self._set(f"exposure={c.exposure},analogue_gain={c.analogue_gain}")
        if c.gain > 256 or (force_gain and not self.gain_unsupported):
            try:
                self._set(f"gain={c.gain}")
            except OSError:
                if c.gain > 256:
                    raise
                self.gain_unsupported = True

    def read(self, name: str) -> Optional[int]:
        try:
            result = self._call([f"--get-ctrl={name}"])
        except OSError:
            return None
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.split(":")[-1].strip())
        except ValueError:
            return None


class AeLoop:
    """Feeds metered luma into Ae at most every INTERVAL and writes what changes."""

    def __init__(self, writer, start_exposure: Optional[int] = None, start_analogue: Optional[int] = None,
                 log: Callable[[str], None] = lambda s: None):
        exposure = start_exposure if start_exposure is not None else writer.read("exposure")
        analogue = start_analogue if start_analogue is not None else writer.read("analogue_gain")
        self.ae = Ae(exposure if exposure is not None else SOFT_LINES, analogue if analogue is not None else 1024)
        self.writer = writer
        self.log = log
        self.quiet = 0
        self.last_at = -1e18
        self.failed = False
        self.gain_synced = False  # first write also puts the ISP gain where we think it is
        self.last_mean: Optional[float] = None

    def feed(self, mean_luma: float, now: float) -> Optional[Controls]:
        if now - self.last_at < INTERVAL:
            return None
        self.last_at = now
        self.last_mean = mean_luma
        controls = self.ae.step(mean_luma)
        reassert = False
        if controls is None:
            if self.gain_synced and self.quiet < REASSERT_TICKS:
                self.quiet += 1
                return None
            controls, reassert = self.ae.current(), True
        self.quiet = 0
        try:
            self.writer.write(controls, force_gain=reassert or not self.gain_synced)
            self.gain_synced = True
        except OSError as error:
            if not self.failed:
                self.failed = True
                self.log(f"自動曝光寫不進感光元件，畫面亮度會固定：{error}")
            return None
        return controls


def mean_luma_from_jpeg(jpeg: bytes) -> Optional[float]:
    """Mean 8-bit luma of a JPEG, decoded at 1/4 size (cheap). Needs OpenCV (vision venv)."""
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
    return None if image is None else float(image.mean())
