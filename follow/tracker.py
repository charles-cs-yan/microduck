"""Visual-servo logic: target tracking and the stop-and-go follow cycle. Pure, no I/O.

A line-by-line port of `microduck/vision_follow/core.js` (`boxesFrom`, `Tracker`,
`FollowCycle`) so the behaviour upstream tested on real ducks carries over unchanged.
Detections use the upstream detector's format (`vision_follow/aruco_detector.py`):

    {"width": W, "height": H, "boxes": [{"x0", "y0", "x1", "y1", "score",
      "id"?, "size_norm"?, "distance_status"?, "distance_m"?}, ...]}

Head vectors are `[neck_pitch, head_pitch, head_yaw, head_roll]` (radians), as in
`robot.state.head` and `robot.head`. Output velocities are requests only; `motion.MotionGuard`
clamps and gates them before anything reaches robotd. This module never sets vy, and vyaw
stays 0 here — turning the body is a separate, later step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


@dataclass
class Box:
    x: float          # centre, -1 (left) .. 1 (right)
    y: float          # centre, -1 (top) .. 1 (bottom)
    size: float       # normalised marker size (size_norm) or box height fraction
    area: float
    id: Optional[int]
    distance: Optional[float]  # metres, only when distance_status == "calibrated"
    clipped: bool
    raw: Dict[str, Any] = field(repr=False)


def boxes_from(params: Any) -> List[Box]:
    """Validate and normalise detector output. Anything malformed yields no boxes."""
    if not isinstance(params, dict):
        return []
    width, height, boxes = params.get("width"), params.get("height"), params.get("boxes")
    if not _finite(width) or not _finite(height) or width <= 0 or height <= 0:
        return []
    if not isinstance(boxes, list) or len(boxes) > 256:
        return []
    found = []
    for b in boxes:
        if not isinstance(b, dict):
            continue
        x0, y0, x1, y1, score = (b.get(k) for k in ("x0", "y0", "x1", "y1", "score"))
        if not all(_finite(v) for v in (x0, y0, x1, y1, score)):
            continue
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height or x1 <= x0 or y1 <= y0:
            continue
        w, h = (x1 - x0) / width, (y1 - y0) / height
        ident = b.get("id") if _is_int(b.get("id")) else None
        if w < 0.015 or h < (0.01 if ident is not None else 0.03):
            continue
        size_norm = b.get("size_norm")
        distance = b.get("distance_m")
        found.append(Box(
            x=(x0 + x1) / width - 1,
            y=(y0 + y1) / height - 1,
            size=size_norm if _finite(size_norm) and 0 < size_norm <= 1 else h,
            area=w * h,
            id=ident,
            distance=distance if b.get("distance_status") == "calibrated" and _finite(distance)
            and 0.05 <= distance <= 10 else None,
            clipped=x0 <= 2 or y0 <= 2 or x1 >= width - 2 or y1 >= height - 2,
            raw=b,
        ))
    return found


@dataclass(frozen=True)
class TrackerOptions:
    target_size: float = 0.40
    size_band: float = 0.15
    forward: float = 0.12
    backward: float = 0.10
    timeout: float = 1.2
    deadzone: float = 0.08
    gain: float = 0.7
    head_rate: float = 0.35
    yaw_limit: float = 0.60
    pitch_limit: float = 0.35
    vertical: bool = False
    invert_yaw: bool = False
    invert_pitch: bool = False
    front_gate: bool = True
    target_id: Optional[int] = None
    distance_mode: str = "size"     # "size" or "meters"
    target_distance: float = 0.6
    distance_band: float = 0.08

    def __post_init__(self) -> None:
        for name in ("target_size", "size_band", "forward", "backward", "timeout", "deadzone", "gain",
                     "head_rate", "yaw_limit", "pitch_limit", "target_distance", "distance_band"):
            if not _finite(getattr(self, name)):
                raise ValueError(f"參數 {name} 必須是有限數字")
        o = self
        bad = (o.target_size < .01 or o.target_size > .85 or o.size_band < .05 or o.size_band > .4 or
               o.forward < 0 or o.forward > .25 or o.backward < 0 or o.backward > .25 or
               o.timeout < .6 or o.timeout > 60 or o.gain <= 0 or o.gain > 2 or
               o.head_rate <= 0 or o.head_rate > .6 or o.deadzone < .02 or o.deadzone > .3 or
               o.yaw_limit <= 0 or o.yaw_limit > .7 or o.pitch_limit <= 0 or o.pitch_limit > .4 or
               o.target_distance < .2 or o.target_distance > 3 or o.distance_band < .03 or o.distance_band > .3 or
               o.distance_mode not in ("size", "meters") or
               any(not isinstance(getattr(o, k), bool) for k in ("front_gate", "vertical", "invert_yaw", "invert_pitch")) or
               (o.target_id is not None and (not _is_int(o.target_id) or o.target_id < 0 or o.target_id > 49)))
        if bad:
            raise ValueError("參數超出展示模組限制")


@dataclass(frozen=True)
class Decision:
    head: List[float]
    vx: float
    desired_vx: float
    reason: str
    valid: bool
    vy: float = 0.0
    vyaw: float = 0.0
    phase: Optional[str] = None


class Tracker:
    def __init__(self, options: Optional[TrackerOptions] = None, **overrides: Any):
        self.configure(replace(options or TrackerOptions(), **overrides))
        self.reset()

    def configure(self, options: TrackerOptions) -> None:
        if not isinstance(options, TrackerOptions):
            raise TypeError("options must be TrackerOptions")
        self.o = options

    def reset(self, head: Optional[List[float]] = None) -> None:
        head = [0.0, 0.0, 0.0, 0.0] if head is None else head
        if not isinstance(head, list) or len(head) != 4 or not all(_finite(v) for v in head):
            raise ValueError("頭部初值無效")
        self.head = [float(v) for v in head]
        self.aim = list(self.head)
        self.target: Optional[Box] = None
        self.last_detection = -math.inf
        self.last_update: Optional[float] = None
        self.confirmed = 0
        self.velocity = 0.0
        self.filtered_size: Optional[float] = None
        self.direction = 0
        self.serial = 0
        self.filtered_distance: Optional[float] = None

    def _lose(self) -> None:
        self.target = None
        self.confirmed = 0
        self.velocity = 0.0
        self.direction = 0
        self.filtered_size = None
        self.last_update = None
        self.filtered_distance = None

    def ingest(self, params: Any, now: float) -> Optional[Box]:
        o = self.o
        found = [b for b in boxes_from(params) if o.target_id is None or b.id == o.target_id]
        # Two physical cards with the same ID cannot be told apart safely.
        if o.target_id is not None and len(found) > 1:
            found = []
        previous = self.target
        if previous is not None and now - self.last_detection <= o.timeout:
            matches = [b for b in found if b.id == previous.id
                       and math.hypot(b.x - previous.x, b.y - previous.y) < .65
                       and .45 < b.size / previous.size < 2.2]
            matches.sort(key=lambda b: math.hypot(b.x - previous.x, b.y - previous.y))
            selected = matches[0] if matches else None
        else:
            found.sort(key=lambda b: math.hypot(b.x, b.y))
            selected = found[0] if found else None
            self.confirmed = 0
            self.filtered_size = None
            self.last_update = None
            self.filtered_distance = None
        if selected is None:
            self._lose()
            return None
        dt = 0.0 if self.last_update is None else _clamp(now - self.last_update, 0, .6)
        self.target = selected
        self.last_detection = now
        self.last_update = now
        self.confirmed += 1
        self.serial += 1
        self.filtered_size = selected.size if self.filtered_size is None else .6 * selected.size + .4 * self.filtered_size
        if selected.distance is None:
            self.filtered_distance = None
        elif self.filtered_distance is None:
            self.filtered_distance = selected.distance
        else:
            self.filtered_distance = .6 * selected.distance + .4 * self.filtered_distance
        # Integrate only once per *new detection*, never repeatedly against an old image.
        if self.confirmed >= 2:
            def step(error: float) -> float:
                return 0.0 if abs(error) < o.deadzone else _clamp(o.gain * error, -o.head_rate, o.head_rate) * dt
            self.aim[2] = _clamp(self.aim[2] + (1 if o.invert_yaw else -1) * step(selected.x), -o.yaw_limit, o.yaw_limit)
            if o.vertical:
                self.aim[1] = _clamp(self.aim[1] + (-1 if o.invert_pitch else 1) * step(selected.y), -o.pitch_limit, o.pitch_limit)
        return selected

    def calibrate(self, now: float) -> float:
        o = self.o
        if (self.target is None or self.confirmed < 2 or now - self.last_detection > o.timeout
                or self.target.clipped):
            raise ValueError("請先讓完整目標穩定出現兩幀，再標定距離")
        if o.distance_mode == "meters" and self.target.distance is None:
            raise ValueError("目前目標沒有有效的標定距離，不能設為公尺目標")
        if o.distance_mode == "meters":
            self.configure(replace(o, target_distance=self.target.distance))
        else:
            self.configure(replace(o, target_size=self.target.size))
        self.velocity = 0.0
        self.direction = 0
        return self.o.target_distance if self.o.distance_mode == "meters" else self.o.target_size

    def output(self, now: float, mode: str, dt: float) -> Decision:
        """mode: "gaze" (head only) or "follow" (head + forward/backward)."""
        o = self.o
        reason, desired = "原地注視", 0.0
        valid = self.target is not None and self.confirmed >= 2 and now - self.last_detection <= o.timeout
        if valid:
            limit = o.head_rate * _clamp(dt, 0, .15)
            for i in (1, 2):
                self.head[i] += _clamp(self.aim[i] - self.head[i], -limit, limit)
        else:
            self.aim = list(self.head)
            reason = "目標遺失或等待連續兩幀"
        if valid and mode == "follow":
            t = self.target
            metric = o.distance_mode == "meters"
            if t.clipped or t.size > .9:
                reason = "目標被裁切，停止平移"
            elif o.front_gate and (abs(t.x) > .35 or abs(self.head[2]) > .22):
                reason = "目標偏離機身正前方，只轉頭"
            elif metric and (t.distance is None or self.filtered_distance is None):
                reason = "沒有有效的標定距離，停止平移"
            else:
                # A Schmitt band prevents forward/backward chatter at the preferred size.
                ratio = (2 - self.filtered_distance / o.target_distance) if metric else self.filtered_size / o.target_size
                band = o.distance_band / o.target_distance if metric else o.size_band
                if ratio < 1 - band:
                    self.direction = 1
                elif ratio > 1 + band:
                    self.direction = -1
                elif ((self.direction == 1 and ratio >= 1 - band / 2) or
                      (self.direction == -1 and ratio <= 1 + band / 2)):
                    self.direction = 0
                desired = o.forward if self.direction > 0 else -o.backward if self.direction < 0 else 0.0
                if desired > 0:
                    reason = "目標較遠，前進" if metric else "目標較小，前進"
                elif desired < 0:
                    reason = "目標較近，後退" if metric else "目標較大，後退"
                else:
                    reason = "距離合適，停止"
        # Stop immediately for a gate/loss; acceleration and reversals are limited.
        if not desired:
            self.velocity = 0.0
            self.direction = 0
        else:
            if self.velocity * desired < 0:
                self.velocity = 0.0
            accel = .15 * _clamp(dt, 0, .15)
            self.velocity += _clamp(desired - self.velocity, -accel, accel)
        return Decision(head=list(self.head), vx=self.velocity, desired_vx=desired, reason=reason, valid=bool(valid))


class FollowCycle:
    """Stop → acknowledged zero → settle → new decoded frame → detect → bounded move → stop.

    A detection result is consumed once; polling an old result can never extend a move.
    """

    def __init__(self, move_seconds: float = 2.5, settle_seconds: float = 1.0, max_age: float = 5.0):
        if (not all(_finite(v) for v in (move_seconds, settle_seconds, max_age)) or
                move_seconds < .2 or move_seconds > 4 or settle_seconds < .3 or settle_seconds > 5 or
                max_age < .8 or max_age > 15):
            raise ValueError("停走跟隨參數超出範圍")
        self.move_seconds = move_seconds
        self.settle_seconds = settle_seconds
        self.max_age = max_age
        self.acceleration = .3
        self.reset()

    def reset(self) -> None:
        self.phase = "idle"
        self.capture_after = math.inf
        self.used = -math.inf
        self.deadline = math.inf
        self.settle_until = math.inf
        self.started = math.inf
        self.speed = 0.0
        self.planned_speed = 0.0
        self.peak_speed = 0.0
        self.stop_reason: Optional[str] = None

    def start(self) -> None:
        self.reset()
        self.phase = "stopping"

    def brake(self, reason: str = "外部停止") -> None:
        if self.phase != "idle":
            self.phase = "stopping"
        self.stop_reason = reason
        self.speed = 0.0
        self.capture_after = math.inf

    def acknowledge_stop(self, now: float) -> bool:
        """Call once robotd accepted a zero velocity (robot.stop / robot.move 0 acknowledged)."""
        if self.phase != "stopping":
            return False
        self.phase = "settling"
        self.settle_until = now + self.settle_seconds
        return True

    def can_capture(self, now: float, video_at: float) -> bool:
        """video_at: timestamp of the newest decoded frame. Only post-settle frames count."""
        if self.phase == "settling" and now >= self.settle_until and video_at >= self.settle_until:
            self.phase = "detecting"
            self.capture_after = self.settle_until
        return self.phase == "detecting"

    def accepts(self, captured: Any, now: float) -> bool:
        return (self.phase == "detecting" and _finite(captured) and captured >= self.capture_after
                and captured <= now and now - captured <= self.max_age)

    def update(self, out: Decision, captured: Any, now: float) -> Decision:
        if self.phase == "moving":
            if now >= self.deadline:
                self.brake("移動段到時")
            elif not out.valid:
                self.brake("目標確認過期或失效")
            elif _sign(out.desired_vx) != _sign(self.speed):
                self.brake(out.reason)
        elif self.accepts(captured, now) and captured > self.used and out.valid:
            self.used = captured
            if _finite(out.desired_vx) and 0 < abs(out.desired_vx) <= .25:
                self.phase = "moving"
                self.speed = out.desired_vx
                self.started = now
                self.deadline = now + self.move_seconds
                self.planned_speed = out.desired_vx
                self.peak_speed = 0.0
                self.stop_reason = None
        vx = (_sign(self.speed) * min(abs(self.speed), max(0.0, now - self.started) * self.acceleration)
              if self.phase == "moving" else 0.0)
        self.peak_speed = max(self.peak_speed, abs(vx))
        if self.phase == "moving":
            reason = f"短程{'前進' if self.speed > 0 else '後退'}，到時停步"
        elif self.phase == "stopping":
            reason = "停步，等待零速度確認"
        elif self.phase == "settling":
            reason = "已停步，等待穩定與新畫面"
        else:
            reason = f"停步辨識：{out.reason}"
        return replace(out, vx=float(vx), reason=reason, phase=self.phase)
