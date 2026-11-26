"""Closed-loop in-place turns on robotd's odometry heading. Pure logic, no I/O.

`robot.state.odom.yaw` (v18) is the contact-odometry heading in the frame the IMU chose at
boot: no absolute reference, but exactly what "turn by N degrees" needs. The turn integrates
the wrapped per-tick heading change, so a 180° turn is unambiguous (comparing against a
target angle would flip sign at ±π).

Conventions (duck-ipc-proto): radians, positive vyaw turns left. A target on the right of the
image makes the tracker drive head_yaw negative, so aligning the body means turning by
head_yaw.

A turn fails — and the caller must stop — when the heading is missing, stops changing while
commanded (stuck, slipping, or a dry run), or the turn outlasts its deadline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Tuple

TURNING, DONE, FAILED = "turning", "done", "failed"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def wrap(angle: float) -> float:
    """Wrap to [-π, π)."""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def odom_yaw(state: Any) -> Optional[float]:
    odom = state.get("odom") if isinstance(state, dict) else None
    yaw = odom.get("yaw") if isinstance(odom, dict) else None
    return float(yaw) if _finite(yaw) else None


@dataclass(frozen=True)
class TurnOptions:
    max_rate: float = 0.40      # rad/s; MotionGuard's Limits cap is 0.5
    min_rate: float = 0.15      # rad/s; slower than this the walk policy may not turn at all
    gain: float = 1.0           # rad/s per rad of remaining angle
    accel: float = 0.5          # rad/s², ramp from standstill
    tolerance: float = 0.08     # rad (~4.6°) counts as arrived
    stall_seconds: float = 2.0  # heading unchanged this long while commanded → fail
    stall_angle: float = 0.03   # rad of progress that resets the stall timer
    slack: float = 2.0          # s added to the deadline (nominal time × time_factor + slack)
    time_factor: float = 1.6    # room for a duck that turns slower than commanded
    wrong_way: float = 0.15     # rad turned AGAINST the request → fail (sign/odom mismatch)
    overshoot: float = 0.30     # rad past the target → fail rather than keep correcting

    def __post_init__(self) -> None:
        for name in ("max_rate", "min_rate", "gain", "accel", "tolerance", "stall_seconds", "stall_angle", "slack",
                     "time_factor", "wrong_way", "overshoot"):
            if not _finite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必須是正的有限數字")
        if self.max_rate > 0.5:
            raise ValueError("max_rate 不可超過 0.5 rad/s")
        if self.min_rate > self.max_rate:
            raise ValueError("min_rate 不可大於 max_rate")
        if self.tolerance > 0.3:
            raise ValueError("tolerance 太大")


class Turn:
    def __init__(self, delta: float, start_yaw: Optional[float], now: float,
                 options: TurnOptions = TurnOptions(), label: str = "轉身"):
        if not _finite(delta) or abs(delta) > math.pi + 1e-6:
            raise ValueError("轉角必須是 -π..π 之間的有限數字")
        self.o = options
        self.delta = float(delta)
        self.label = label
        self.started = now
        self.turned = 0.0
        self.last_yaw = start_yaw
        self.progress_at = now
        self.progress_turned = 0.0
        # Nominal: cruise at max_rate plus the ramp up and the slow approach. 180° → about 14 s cap.
        nominal = abs(delta) / options.max_rate + options.max_rate / options.accel
        self.deadline = now + nominal * options.time_factor + options.slack
        self.status = TURNING
        self.reason = ""
        if start_yaw is None:
            self._fail("沒有 odom 航向，不能轉身")

    @property
    def remaining(self) -> float:
        return self.delta - self.turned

    def _fail(self, reason: str) -> None:
        self.status, self.reason = FAILED, reason

    def update(self, now: float, yaw: Optional[float]) -> Tuple[float, str]:
        """Return (vyaw, status). vyaw is 0 unless status is TURNING."""
        if self.status != TURNING:
            return 0.0, self.status
        if yaw is None:
            self._fail("odom 航向遺失")
            return 0.0, self.status
        self.turned += wrap(yaw - self.last_yaw)
        self.last_yaw = yaw
        remaining = self.remaining
        direction = math.copysign(1.0, self.delta)
        if self.turned * direction < -self.o.wrong_way:
            # Turning away from the request: odom's sign is not what we assume, or it re-anchored.
            # Keep going and the "correction" would spin the duck until the deadline.
            self._fail(f"航向往反方向變了 {math.degrees(abs(self.turned)):.0f}°，停止{self.label}"
                       "（請確認 odom.yaw 的正負號）")
            return 0.0, self.status
        if abs(self.turned) > abs(self.delta) + self.o.overshoot:
            self._fail(f"{self.label}轉過頭 {math.degrees(abs(self.turned) - abs(self.delta)):.0f}°，停止")
            return 0.0, self.status
        if abs(remaining) <= self.o.tolerance:
            self.status, self.reason = DONE, f"{self.label}完成（{math.degrees(self.turned):+.0f}°）"
            return 0.0, self.status
        if now >= self.deadline:
            self._fail(f"{self.label}逾時（轉了 {math.degrees(self.turned):+.0f}°／目標 {math.degrees(self.delta):+.0f}°）")
            return 0.0, self.status
        if abs(self.turned - self.progress_turned) >= self.o.stall_angle:
            self.progress_turned, self.progress_at = self.turned, now
        elif now - self.progress_at >= self.o.stall_seconds:
            self._fail(f"航向 {self.o.stall_seconds:g} 秒沒有變化，停止{self.label}")
            return 0.0, self.status
        speed = min(self.o.max_rate, max(self.o.min_rate, self.o.gain * abs(remaining)))
        speed = min(speed, self.o.accel * max(0.0, now - self.started))
        return math.copysign(speed, remaining), self.status


class TimedTurn:
    """Open-loop in-place turn: command `rate` for a fixed time, ignoring odometry.

    11/26 on the duck: robotd applied the full 0.4 rad/s, but odom.yaw (IMU-integrated, no
    magnetometer, half its reads stale) wobbled ±0.1 rad with the gait and drifted the wrong way,
    so the closed-loop Turn aborted within 2 s every time ("wrong way" / "no change"). The
    scripted show only needs "turn round": run the command for `seconds` (calibrated on stage)
    and let the operator stop it. The odom change is logged for calibration, never acted on.
    Same interface as Turn: update(now, yaw) -> (vyaw, status), .remaining, .reason, .label.
    """

    def __init__(self, delta: float, seconds: float, start_yaw: Optional[float], now: float,
                 rate: float = 0.40, accel: float = 0.5, label: str = "轉身"):
        if not _finite(delta) or delta == 0 or abs(delta) > math.pi + 1e-6:
            raise ValueError("轉角必須是 -π..π 之間、非 0 的有限數字")
        if not _finite(seconds) or not 1.0 <= seconds <= 30.0:
            raise ValueError("轉身秒數必須在 1..30")
        if not _finite(rate) or not 0.15 <= rate <= 0.5:
            raise ValueError("轉速必須在 0.15..0.5 rad/s")
        if not _finite(accel) or accel <= 0:
            raise ValueError("accel 必須是正數")
        self.delta = float(delta)
        self.seconds = float(seconds)
        self.rate = float(rate)
        self.accel = float(accel)
        self.label = label
        self.started = now
        self.commanded = 0.0        # integral of the vyaw we sent (rad), for the progress display
        self.last_now = now
        self.start_yaw = start_yaw
        self.last_yaw = start_yaw
        self.odom_turned = 0.0      # logged only
        self.status = TURNING
        self.reason = ""

    @property
    def remaining(self) -> float:
        return math.copysign(max(0.0, abs(self.delta) - abs(self.commanded)), self.delta)

    def update(self, now: float, yaw: Optional[float]) -> Tuple[float, str]:
        if self.status != TURNING:
            return 0.0, self.status
        if yaw is not None and self.last_yaw is not None:
            self.odom_turned += wrap(yaw - self.last_yaw)
        if yaw is not None:
            self.last_yaw = yaw
        elapsed = now - self.started
        if elapsed >= self.seconds:
            self.status = DONE
            self.reason = (f"{self.label}完成（定時 {self.seconds:g} 秒；odom 讀到 "
                           f"{math.degrees(self.odom_turned):+.0f}°，僅供校正）")
            return 0.0, self.status
        speed = min(self.rate, self.accel * max(0.0, elapsed))
        vyaw = math.copysign(speed, self.delta)
        self.commanded += vyaw * max(0.0, now - self.last_now)
        self.last_now = now
        return vyaw, self.status
