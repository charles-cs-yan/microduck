"""Scripted on-stage interactions (head, mouth, sounds) as keyframe routines. Pure logic.

A routine never asks for body velocity; the app holds the duck still while it runs and all
head/mouth intents still pass through MotionGuard (gated on a standing, healthy state).

Head vectors are [neck_pitch, head_pitch, head_yaw, head_roll] in radians, +yaw = left.
Head targets are interpolated linearly between keyframes; mouth values are held until the
next keyframe that sets one; sounds fire once when their keyframe time passes.
Sound tags are robotd v18 `SoundTag`s.

A keyframe may also carry a body `pose` (z m, roll rad, pitch rad) for robot.pose. It must stay
inside the trained range (posture.Z_RANGE / TILT_LIMIT) and is interpolated like the head,
starting from the neutral (0, 0, 0). A routine with `loop_until` repeats its frames until that
many seconds have passed (the dance); the frames should then end where they start.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .posture import TILT_LIMIT, Z_RANGE

SOUND_TAGS = ("alarm", "greet", "inquire", "peck", "chirp", "coo")  # the board's bank; not "wheee": it needs a held stream (no "laugh" in the bank)


@dataclass(frozen=True)
class Keyframe:
    t: float
    head: Optional[Tuple[float, float, float, float]] = None
    mouth: Optional[float] = None
    sound: Optional[str] = None
    pose: Optional[Tuple[float, float, float]] = None  # (z, roll, pitch)


NEUTRAL_POSE = (0.0, 0.0, 0.0)


def _validate(frames: Sequence[Keyframe]) -> None:
    last = -math.inf
    for frame in frames:
        if not math.isfinite(frame.t) or frame.t < last:
            raise ValueError("keyframe 時間必須遞增")
        last = frame.t
        if frame.sound is not None and frame.sound not in SOUND_TAGS:
            raise ValueError(f"不支援的聲音 {frame.sound!r}")
        if frame.mouth is not None and not 0.0 <= frame.mouth <= 1.0:
            raise ValueError("mouth 必須在 0..1")
        if frame.head is not None and (len(frame.head) != 4 or not all(math.isfinite(v) for v in frame.head)):
            raise ValueError("head 必須是 4 個有限數字")
        if frame.pose is not None:
            if len(frame.pose) != 3 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in frame.pose):
                raise ValueError("pose 必須是 3 個有限數字 (z, roll, pitch)")
            z, roll, pitch = frame.pose
            if not (Z_RANGE[0] <= z <= Z_RANGE[1] and abs(roll) <= TILT_LIMIT and abs(pitch) <= TILT_LIMIT):
                raise ValueError("pose 超出訓練範圍")
    if not frames:
        raise ValueError("routine 不能是空的")


def _chatter(start: float, count: int, period: float = 0.3, open_: float = 0.8) -> List[Keyframe]:
    frames = []
    for i in range(count):
        frames.append(Keyframe(start + i * period, mouth=open_))
        frames.append(Keyframe(start + i * period + period / 2, mouth=0.0))
    return frames


def _merge(*groups: Sequence[Keyframe]) -> List[Keyframe]:
    merged: dict = {}
    for frame in (f for g in groups for f in g):
        prev = merged.get(frame.t)
        if prev is None:
            merged[frame.t] = frame
        else:
            merged[frame.t] = Keyframe(frame.t, frame.head or prev.head,
                                       frame.mouth if frame.mouth is not None else prev.mouth,
                                       frame.sound or prev.sound, frame.pose or prev.pose)
    return [merged[t] for t in sorted(merged)]


CENTRE = (0.0, 0.0, 0.0, 0.0)

# "Hello everyone": look left at the audience and quack, look right and chirp, back to centre.
GREETING: List[Keyframe] = _merge(
    [Keyframe(0.0, head=CENTRE, sound="greet"),
     Keyframe(0.9, head=(0.0, 0.15, 0.45, 0.0)),
     Keyframe(2.0, head=(0.0, 0.15, 0.45, 0.0)),
     Keyframe(2.9, head=(0.0, 0.15, -0.45, 0.0), sound="chirp"),
     Keyframe(4.0, head=(0.0, 0.15, -0.45, 0.0)),
     Keyframe(4.8, head=CENTRE, mouth=0.0)],
    _chatter(0.1, 3),
    _chatter(3.0, 2),
)
_validate(GREETING)

# Money-gun celebration: sway side to side, then bob and nod, head swinging against the body,
# beak chattering. Stays on the spot (no velocity). One lap is 4 s and ends where it starts so
# it loops cleanly. Amounts stay well inside the trained range (|roll| 0.15 of 0.26, z -0.015).
# The roll SIGN is unverified on the robot; the dance is symmetric so it does not matter.
DANCE_LAP = 4.0
DANCE_MAX = 30.0  # s; the dance ends itself after this even if nobody presses anything
_SWAY = 0.15
_DIP = -0.012
DANCE: List[Keyframe] = _merge(
    [Keyframe(0.0, head=CENTRE, pose=NEUTRAL_POSE, sound="greet"),
     Keyframe(0.5, head=(0.0, 0.1, -0.30, -0.15), pose=(_DIP, _SWAY, 0.0)),
     Keyframe(1.0, head=CENTRE, pose=NEUTRAL_POSE, sound="chirp"),
     Keyframe(1.5, head=(0.0, 0.1, 0.30, 0.15), pose=(_DIP, -_SWAY, 0.0)),
     Keyframe(2.0, head=CENTRE, pose=NEUTRAL_POSE, sound="coo"),
     Keyframe(2.5, head=(0.0, -0.20, 0.0, 0.0), pose=(-0.015, 0.0, 0.08)),
     Keyframe(3.0, head=CENTRE, pose=NEUTRAL_POSE, sound="chirp"),
     Keyframe(3.5, head=(0.0, -0.20, 0.0, 0.0), pose=(-0.015, 0.0, 0.08)),
     Keyframe(DANCE_LAP, head=CENTRE, pose=NEUTRAL_POSE, mouth=0.0)],
    _chatter(0.1, 3, period=0.25),
    _chatter(2.1, 3, period=0.25),
)
_validate(DANCE)


class Routine:
    def __init__(self, frames: Sequence[Keyframe], now: float, start_head: Optional[Sequence[float]] = None,
                 name: str = "打招呼", loop_until: Optional[float] = None):
        _validate(frames)
        if loop_until is not None and (not math.isfinite(loop_until) or loop_until <= 0 or frames[-1].t <= 0):
            raise ValueError("loop_until 必須是正數，且 routine 要有長度")
        self.frames = list(frames)
        self.started = now
        self.name = name
        self.start_head = tuple(start_head) if start_head is not None and len(start_head) == 4 else CENTRE
        self._fired = 0  # index of the next keyframe whose sound has not fired
        self._lap = 0
        self.loop_until = loop_until
        self.duration = self.frames[-1].t if loop_until is None else loop_until
        self.uses_pose = any(f.pose is not None for f in self.frames)
        self.pose: Optional[Tuple[float, float, float]] = None  # body pose target, None if unused

    def _interp(self, t: float, attr: str, start):
        t0, v0, target = 0.0, start, None
        for frame in self.frames:
            value = getattr(frame, attr)
            if frame.t <= t:
                if value is not None:
                    t0, v0 = frame.t, value
            elif value is not None:
                target = frame
                break
        if target is None:
            return tuple(v0)
        span = target.t - t0
        alpha = 1.0 if span <= 0 else min(1.0, max(0.0, (t - t0) / span))
        return tuple(a + (b - a) * alpha for a, b in zip(v0, getattr(target, attr)))

    def update(self, now: float) -> Tuple[Tuple[float, float, float, float], float, List[str], bool]:
        """Return (head target, mouth, sounds to play now, done). Also sets `self.pose`."""
        elapsed = max(0.0, now - self.started)
        done = elapsed >= self.duration
        t = elapsed
        sounds: List[str] = []
        if self.loop_until is not None:
            length = self.frames[-1].t
            lap = int(min(elapsed, self.duration) // length)
            t = min(elapsed, self.duration) - lap * length
            if lap != self._lap:
                # A new lap: the rest of the previous lap's sounds are dropped (never a burst).
                self._lap, self._fired = lap, 0
        while self._fired < len(self.frames) and self.frames[self._fired].t <= t:
            if self.frames[self._fired].sound:
                sounds.append(self.frames[self._fired].sound)
            self._fired += 1
        head = self._interp(t, "head", self.start_head)
        mouth = 0.0
        for frame in self.frames:
            if frame.t > t:
                break
            if frame.mouth is not None:
                mouth = frame.mouth
        self.pose = self._interp(t, "pose", NEUTRAL_POSE) if self.uses_pose else None
        return tuple(head), (0.0 if done else mouth), sounds, done
