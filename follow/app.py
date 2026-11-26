"""Main program: operator commands → gaze / stop-and-go follow, through MotionGuard.

Modes
    idle    standing still, head not driven (start-up state, and after "stop")
    gaze    head tracks the marker, body does not move
    follow  head tracks; FollowCycle makes bounded forward/backward pulses toward the marker.
            When the marker is centred in the image but the head has turned past
            `align_threshold`, the body first turns in place (closed loop on odom yaw) to face it.
    home    turn round in place (default +180°, left), then switch to follow — the host has
            turned too and walks off stage ahead of the duck. `--home-turn 0` skips the turn.
    greet   scripted head / mouth / sounds, then back to the previous mode.
    dance   on the spot: body sway/bob (robot.pose) + head + mouth + sounds, looped until the
            next command or greet.DANCE_MAX (30 s); then back to gaze (if it came from gaze) or idle.
    sit     robot.do sit_toggle sent; wait for robotd's "sit".
    crouch  "趴下": the deepest trained body pose (robotd has no lying skill), held until another command.
    skill   a one-shot robotd skill or a rise is running; robotd finishes it, we wait for "stand".

Commands — keyboard (always) and English voice (see sources.KEY_ALIASES / speech.LABELS):
follow / gaze / stop / home / greet / dance / quit, sit / stand / crouch, pick / roll / kick_left / kick_right.
Posture safety (follow/posture.py): only from standing still on a fresh state; nothing new while
robotd is busy (robot.stop does not cancel a skill); risky keys twice in a row; skills and the
floor bring-up are keyboard-only; voice posture is refused mid-follow / mid-home.

Turns run on robot.state.odom.yaw. In a dry run nothing moves, so the heading never changes
and every turn fails after `stall_seconds` (2 s) — that is the expected dry-run behaviour.

The tick mirrors vision_follow/app.js `tick()` in step mode: readiness check → capture only
when the cycle allows → tracker → cycle → acknowledged zero before every detection.

Safety
* Every velocity/head command goes through MotionGuard (limits, gate, deadman, stop on exit).
* Detections that are not from a live camera on this robot never drive it: the guard runs in
  dry-run (sends nothing that moves a motor, and no sound) unless `--live` AND `--camera` are
  given AND the operator types "yes" at start-up.

    python3 -m follow.app --replay detections.jsonl        # dry run against the real robotd
    python3 -m follow.app --camera /dev/videoN             # live camera, still a dry run
    python3 -m follow.app --camera /dev/videoN --live      # moves the robot (asks first)
"""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .motion import MotionGuard, install_signal_handlers
from .robotd_client import DEFAULT_SOCKET, RobotdClient, RobotdError
from .safety import Limits, hard_problem
from .sources import (COMMANDS, CommandQueue, Frame, NullSource, ProcessDetectionSource, Recorder, ReplaySource,
                      parse_command)
from .speech import VoiceCommands, default_python
from .status import StatusPrinter, compose
from .tracker import Decision, FollowCycle, Tracker, TrackerOptions
from .turning import DONE, FAILED, TURNING, Turn, TurnOptions, odom_yaw
from .greet import DANCE, DANCE_MAX, GREETING, Routine
from .posture import (BUSY_POLICIES, CROUCH, SITTING_POLICIES, STANDING_POLICIES, STATE_FRESH,
                      bring_up_problem, rise_problem, still)

# Voice feedback: the call made when a spoken command switches mode (greet/dance have their own).
ACK_SOUNDS = {"follow": "chirp", "gaze": "peck", "home": "coo", "idle": "coo", "straight": "chirp"}
# `k`: walk straight ahead a fixed distance, no camera. Odometry measures the distance; the
# timeout is the backstop because odom under-reads at low speed (0.12 m/s × 3 s read 0.22 m).
# No heading correction: odom.yaw's sign is not verified on this duck yet.
STRAIGHT_DISTANCE = 2.0  # m
STRAIGHT_TIMEOUT = 35.0  # s; at the ~0.07 m/s the duck really makes, 2 m takes ~30 s
LOST_HINT_AFTER = 4.0    # s without a marker in follow/gaze before the duck "asks"
LOST_HINT_REPEAT = math.inf  # once per mode: repeated calls drown out the host's next command
MODES = ("idle", "gaze", "follow", "home", "greet", "sit", "crouch", "skill", "dance", "straight")
POSTURE_COMMANDS = ("sit", "stand", "crouch", "pick", "roll", "kick_left", "kick_right", "dance")
VISION_WORKER = Path(__file__).resolve().parent / "vision" / "aruco_worker.py"
RELAY_WORKER = Path(__file__).resolve().parent / "vision" / "relay_worker.py"
ZERO_HEAD = [0.0, 0.0, 0.0, 0.0]


TRACKER_FORWARD_MAX = 0.25  # TrackerOptions refuses more; MotionGuard's own cap (--forward) is separate


def default_tracker_options(target_id: Optional[int] = 0, forward: float = 0.12) -> TrackerOptions:
    # vision_follow step-mode defaults: front gate off, target kept 12 s (must exceed
    # max_age 5 + move 2.5 + 0.2 so a pulse never outlives its own detection).
    # `forward` is the speed a pulse asks for; without it --forward only raised the guard's cap
    # and the duck still walked at the tracker's 0.12.
    return TrackerOptions(front_gate=False, target_id=target_id, timeout=12.0,
                          forward=min(forward, TRACKER_FORWARD_MAX))


class FollowApp:
    def __init__(self, guard: MotionGuard, source, commands: CommandQueue, *,
                 tracker: Optional[Tracker] = None, cycle: Optional[FollowCycle] = None,
                 video_timeout: float = 10.0, stall_seconds: float = 0.4, moving_frame_timeout: float = 1.5,
                 turn_options: TurnOptions = TurnOptions(), align_threshold: float = 0.30,
                 home_angle: float = math.pi, scripted: bool = False, walk_m: float = STRAIGHT_DISTANCE,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        if not 0.15 <= align_threshold <= 0.6:
            raise ValueError("align_threshold 必須在 0.15..0.6 rad（頭部 yaw 上限是 0.6）")
        if not math.isfinite(home_angle) or abs(home_angle) > math.pi + 1e-6 or 0 < abs(home_angle) < 0.5:
            # 0 = no fixed turn: "home" just follows the host and the align turn faces them
            raise ValueError("home_angle 必須是 0 或 ±0.5..±π rad")
        self.guard = guard
        self.source = source
        self.commands = commands
        self.tracker = tracker or Tracker(default_tracker_options())
        self.cycle = cycle or FollowCycle()
        self.video_timeout = video_timeout
        self.stall_seconds = stall_seconds
        # While a pulse runs, frames arrive every 1/hz s (0.5 s at 2 Hz). Three missed frames → brake
        # now instead of finishing the 2.5 s pulse blind (review: worker death mid-pulse).
        self.moving_frame_timeout = moving_frame_timeout
        self.turn_options = turn_options
        self.align_threshold = align_threshold
        self.home_angle = home_angle
        # Scripted show (no camera): "follow" walks straight walk_m metres; "home" turns, then walks
        # straight back. on_stage remembers which of the two "Let's go" means.
        self.scripted = scripted
        self.on_stage = False
        self.going_home = False
        self.straight_distance = walk_m
        self.straight_timeout = STRAIGHT_TIMEOUT
        self.straight_from = (None, None, 0.0)
        self.turn: Optional[Turn] = None
        self.turn_kind: Optional[str] = None   # "align" or "home"
        self.turn_head = list(ZERO_HEAD)
        self.routine: Optional[Routine] = None
        self.return_mode = "idle"
        self.recorder = None  # sources.Recorder: every new frame is saved for --replay
        # Audible feedback, so the host on stage knows the duck heard him and what it is doing.
        self.mode_since = 0.0
        self.last_target_at = -math.inf
        self.lost_hint_at = -math.inf
        self.skill: Optional[dict] = None
        self.sit_started = -math.inf
        self.crouch_started = -math.inf
        self._pending_confirm: Optional[str] = None
        self._pending_at = -math.inf
        self._deferred_dance = -math.inf  # voice dance heard mid-follow: start once stood still, until then
        self._clock = clock
        self._log = log
        self.mode = "idle"
        self.running = True
        self.last_ingested = -math.inf
        self.last_detection_at = -math.inf
        self.last_tick: Optional[float] = None
        self.last_decision: Optional[Decision] = None
        self.problem = ""
        self._last_reason = None

    # ── commands ─────────────────────────────────────────────────────────────
    def handle(self, command: str, source: str = "keyboard") -> None:
        before = self.mode
        self._handle(command, source)
        if source == "voice":
            self._acknowledge(command, before)

    def _acknowledge(self, command: str, before: str) -> None:
        """A short call when a spoken command changed what the duck does; a questioning one when not.

        Greeting and dancing make their own sounds from their first keyframe, so they get none here.
        A command that changed nothing (refused, or already in that mode) gets "inquire"."""
        if self.mode in ("greet", "dance"):
            return
        if self.mode != before or self._deferred_dance > self._clock():  # a deferred dance is a yes too
            tag = ACK_SOUNDS.get(self.mode, "chirp")
        else:
            tag = "inquire"
        refused = self.guard.play(tag)
        if refused:
            self._log(f"回應叫聲沒播出：{refused}")

    def _handle(self, command: str, source: str = "keyboard") -> None:
        if command != self._pending_confirm:
            # "press twice" means twice IN A ROW: 2 → s → 2 or 2 → f → 2 must not roll the duck
            self._pending_confirm = None
        self._deferred_dance = -math.inf  # any newer command wins over a dance still waiting to start
        if self.scripted and command == "letsgo":
            # No camera to read the stage from: "Let's go" means out when off stage, back when on it.
            command = "home" if self.on_stage else "follow"
        if self.scripted and command == "follow":
            command = "straight"
        if command == "letsgo":
            # "LET'S GO" is a prefix of "LET'S GO HOME" (the model reports the longer phrase when it
            # hears it, but a clipped "home" leaves just this). So it means: off stage → come with me;
            # standing on stage (gaze / dance) → go home; while walking → nothing (keep going).
            if self.mode == "idle":
                command = "follow"
            elif self.mode in ("gaze", "dance"):
                command = "home"
            else:
                self._log(f"「LET'S GO」在 {self.mode} 時不做事（要回家請說 LET'S GO HOME 或按 h）")
                return
        if (command == "home" and source == "voice" and not self.scripted
                and self.mode not in ("follow", "gaze", "dance")):
            # "home" turns the duck 180° without needing a target. A misheard "go home" while it
            # stands idle must not spin it: by voice only mid-show (follow/gaze/dance); `h` always works.
            self._log("語音「回家」只在跟隨或注視時接受；待命時請按 h")
            return
        if command == "follow":
            self.set_mode("follow", "指令：跟隨")
        elif command == "gaze":
            self.set_mode("gaze", "指令：注視")
        elif command == "stop":
            self.set_mode("idle", "指令：停止")
        elif command == "home":
            self.set_mode("home", "指令：回家")
        elif command == "greet":
            if self.mode != "greet":
                self.return_mode = self.mode if self.mode in ("gaze", "follow") else "idle"
            self.set_mode("greet", "指令：打招呼")
        elif command == "straight":
            self.set_mode("straight", f"指令：直走 {self.straight_distance:g} 公尺")
        elif command == "quit":
            self.set_mode("idle", "指令：結束")
            self.running = False
        elif command in POSTURE_COMMANDS:
            self._posture(command, source)

    # ── posture & skills (follow/posture.py says what robotd offers) ─────────
    def _standing(self) -> bool:
        state = self.guard.client.state or {}
        safety = state.get("safety") or {}
        return (state.get("policy") in STANDING_POLICIES and safety.get("fallen") is False
                and safety.get("limp") is False)

    def _confirmed(self, command: str) -> bool:
        """Risky commands need the same key twice within 3 s (an accidental key must not roll the duck)."""
        now = self._clock()
        if self._pending_confirm == command and now - self._pending_at <= 3.0:
            self._pending_confirm = None
            return True
        self._pending_confirm, self._pending_at = command, now
        return False

    def _ready_for_posture(self, label: str) -> bool:
        """Standing STILL on a fresh state, nothing latched. Walking modes are stopped first and
        the request refused: the operator asks again once the duck stands still."""
        client = self.guard.client
        state, age = client.state, client.state_age()
        if self.guard.failed:
            self._log(f"不能{label}：{self.guard.failed}")
            return False
        if not isinstance(state, dict) or age is None or age > STATE_FRESH:
            self._log(f"不能{label}：機器人狀態沒有更新")
            return False
        if self.mode in ("follow", "home", "gaze", "greet", "dance") or self.guard.moving:
            self.set_mode("idle", f"準備{label}")
            if not still(state):
                self._log(f"先停下了；等站穩後再下一次「{label}」")
                return False
        if not still(state):
            self._log(f"現在不是站穩不動的狀態（policy={state.get('policy')}），不能{label}")
            return False
        return True

    def _posture(self, command: str, source: str) -> None:
        state = self.guard.client.state or {}
        policy = state.get("policy")
        if self.mode == "skill" or policy in BUSY_POLICIES:
            # robot.stop does NOT cancel a skill: robotd finishes it whatever we do. Nothing new
            # may be asked meanwhile — sit_toggle is a toggle, enable would clear the skill.
            what = (self.skill or {}).get("label") or policy
            self._log(f"{what}還在進行：robotd 會把它做完（s 只會停止 follow 的控制），等它做完再下指令")
            return
        if self.mode == "sit" and not (command == "stand" and policy in SITTING_POLICIES):
            # after sit_toggle is accepted robotd may still report "stand" for a cycle or two
            self._log("還在坐下（等它坐好），先別下其他姿態指令")
            return
        if source == "voice" and self.mode in ("follow", "home") and not (command == "dance" and self.mode == "follow"):
            self._log("跟隨或回家途中不接受姿態語音（對觀眾說的 sit down 也會被聽到）；要做請按鍵")
            return
        if command == "sit":
            if policy in SITTING_POLICIES:
                self._log("已經坐著")
            elif self._ready_for_posture("坐下"):
                self.set_mode("sit", "指令：坐下")
                refused = self.guard.do_skill("sit_toggle")
                if refused:
                    self._log(f"坐下沒有送出：{refused}")
                    self.set_mode("idle", "坐下失敗")
            return
        if command == "stand":
            client = self.guard.client
            health_age = None if self.guard.health_at is None else self._clock() - self.guard.health_at
            if policy in SITTING_POLICIES:
                problem = rise_problem(state, client.state_age(), self.guard.health, health_age, self.guard.failed)
                if problem:
                    self._log(f"不能從坐姿站起來：{problem}")
                    return
                self._start_skill("sit_toggle", "從坐姿站起來", timeout=10.0)
                return
            if self._standing():
                self._log("已經站著")
                if self.mode == "crouch":
                    self.set_mode("idle", "已經站著")
                return
            # held / fallen / limp: the bring-up. Operator's key only, checked, pressed twice.
            if source == "voice":
                self._log("從地上站起來只接受鍵盤：扶好鴨子後連按兩次 u")
                return
            problem = bring_up_problem(state, client.state_age(), self.guard.health, health_age, self.guard.failed)
            if problem:
                self._pending_confirm = None
                self._log(f"不能從地上站起來：{problem}")
                return
            if not self._confirmed("stand"):
                self._log("⚠ 要從地上站起來：扶好鴨子，3 秒內再按一次 u")
                return
            self.set_mode("skill", "指令：站起來")
            refused = self.guard.bring_up()
            if refused:
                self._log(f"站起來沒有送出：{refused}")
                self.set_mode("idle", "站起來失敗")
                return
            self.skill = {"name": "bring_up", "label": "從地上站起來", "started": self._clock(),
                          "timeout": 20.0, "left_standing": True}
            return
        if command == "dance":
            if self.mode == "dance":
                self._log("已經在跳舞")
                return
            back = "gaze" if self.mode in ("gaze", "follow") and not self.scripted else "idle"
            was_follow = self.mode == "follow"
            if self._ready_for_posture("跳舞"):
                self.return_mode = back
                self.set_mode("dance", "指令：跳舞")
            elif was_follow and self.mode == "idle":
                # Stopped mid-pulse: a voice command cannot be "said again" by the operator, so
                # start by itself once the duck stands still (within 3 s; any other command cancels).
                self._deferred_dance = self._clock() + 3.0
                self._log("跳舞：先停穩，3 秒內站穩就開始跳")
            return
        if command == "crouch":
            if self._ready_for_posture("趴下"):
                self.set_mode("crouch", "指令：趴下")
            return
        skill, label = {"pick": ("ground_pick", "撿東西"), "roll": ("roulade", "向前翻滾"),
                        "kick_left": ("kick_left", "左腳踢"), "kick_right": ("kick_right", "右腳踢")}[command]
        if source == "voice":
            self._log(f"「{label}」只接受鍵盤")
            return
        if not self._ready_for_posture(label):
            self._pending_confirm = None
            return
        if command in ("roll", "kick_left", "kick_right") and not self._confirmed(command):
            self._log(f"⚠ {label}：確認前方淨空，3 秒內再按一次同一個鍵")
            return
        self._start_skill(skill, label, timeout=8.0)

    def _start_skill(self, skill: str, label: str, timeout: float) -> None:
        self.set_mode("skill", f"指令：{label}")
        refused = self.guard.do_skill(skill)
        if refused:
            self._log(f"{label}沒有送出：{refused}")
            self.set_mode("idle", f"{label}失敗")
            return
        self.skill = {"name": skill, "label": label, "started": self._clock(), "timeout": timeout,
                      "left_standing": False}
        self._log(f"{label}：已送出，等它做完並站穩")

    def _tick_skill(self, now: float) -> Decision:
        """Wait for a one-shot skill / rise to end standing. robotd runs it; we only watch.
        No hard-problem check here: a roll or a rise legitimately passes through fallen/other policies."""
        self.guard.halt()
        skill = self.skill or {}
        state = self.guard.client.state or {}
        policy = state.get("policy")
        elapsed = now - skill.get("started", now)
        if policy not in STANDING_POLICIES:
            skill["left_standing"] = True
        reason = f"{skill.get('label', '技能')}進行中（policy={policy}，{elapsed:.1f} 秒）"
        if policy == "protection":
            self._log(f"✗ {skill.get('label')}後 robotd 進入保護：扶住鴨子，必要時 robotctl robot relax --yes")
            self.set_mode("idle", "技能失敗")
        elif self._standing() and elapsed >= 0.5 and (skill.get("left_standing") or elapsed > 2.0):
            self._log(f"✓ {skill.get('label')}完成，站穩了")
            self.set_mode("idle", "技能完成")
        elif elapsed > skill.get("timeout", 8.0):
            self._log(f"✗ {skill.get('label')} {elapsed:.0f} 秒還沒站穩（policy={policy}）：扶住它，可按 u 站起來")
            self.set_mode("idle", "技能逾時")
        out = Decision(head=list(ZERO_HEAD), vx=0.0, desired_vx=0.0, reason=reason, valid=True, phase="skill")
        self.last_decision = out
        return out

    def _tick_sit(self, now: float) -> Decision:
        self.guard.halt()
        state = self.guard.client.state or {}
        policy, safety = state.get("policy"), state.get("safety") or {}
        elapsed = now - self.sit_started
        if safety.get("fallen") is True or safety.get("limp") is True or policy == "protection":
            self._log(f"坐著時倒地／卸力／保護（policy={policy}）：回到待命")
            self.set_mode("idle", "坐姿異常")
        elif policy in SITTING_POLICIES or policy == "rise":
            pass  # sitting (or rising after someone else toggled)
        elif elapsed > 8.0:
            self._log(f"送出坐下 8 秒後還是 policy={policy}：回到待命")
            self.set_mode("idle", "坐下沒有發生")
        out = Decision(head=list(ZERO_HEAD), vx=0.0, desired_vx=0.0, reason=f"坐著（policy={policy}）",
                       valid=True, phase="sit")
        self.last_decision = out
        return out

    def _tick_crouch(self, now: float) -> Decision:
        self.guard.halt()
        alpha = min(1.0, max(0.0, (now - self.crouch_started) / 1.5))  # glide down over 1.5 s
        self.guard.set_pose(CROUCH["z"] * alpha, CROUCH["roll"] * alpha, CROUCH["pitch"] * alpha)
        out = Decision(head=list(ZERO_HEAD), vx=0.0, desired_vx=0.0,
                       reason="趴下" if alpha >= 1.0 else "趴下中", valid=True, phase="crouch")
        self.last_decision = out
        return out

    def set_mode(self, mode: str, reason: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        if mode == self.mode:
            return
        self._log(f"模式 {self.mode} → {mode}（{reason}）")
        if self.mode in ("crouch", "dance"):
            self.guard.release_pose()
        if self.mode in ("greet", "dance"):
            self.guard.set_mouth(0.0)
        self._pending_confirm = None  # a mode change (incl. a fall → idle) voids a half-confirmed key
        self.turn, self.turn_kind = None, None
        self.routine = None
        self.skill = None
        self.cycle.reset()
        self.guard.confirm_stopped()
        if mode == "sit":
            self.sit_started = self._clock()
        if mode == "crouch":
            self.crouch_started = self._clock()
        if mode == "follow":
            self.cycle.start()
        if mode in ("follow", "gaze"):
            try:
                self.tracker.reset(self._state_head())
            except ValueError:
                self.tracker.reset()
            self.last_detection_at = -math.inf
        self.mode = mode
        self.mode_since = self._clock()
        self.last_target_at = -math.inf
        self.lost_hint_at = -math.inf
        if mode == "home":
            # Host says "let's go home": turn round in place, then follow the host off stage.
            if self.home_angle == 0:
                self.set_mode("follow", "回家：不做固定轉身，直接跟隨主持人")
            else:
                self._start_turn("home", self.home_angle, self._clock(), self._state_head())
        if mode == "straight":
            odom = (self.guard.client.state or {}).get("odom") or {}
            pos = odom.get("position")
            self.straight_from = (list(pos[:2]) if isinstance(pos, list) and len(pos) >= 2 else None,
                                  odom_yaw(self.guard.client.state), self._clock())
        if mode == "greet":
            self.routine = Routine(GREETING, self._clock(), self._state_head())
        if mode == "dance":
            self.routine = Routine(DANCE, self._clock(), self._state_head(), name="跳舞", loop_until=DANCE_MAX)

    # ── scripted interaction ─────────────────────────────────────────────────
    def _tick_straight(self, now: float, stalled: bool, gap) -> Optional[Decision]:
        """Walk straight ahead until odometry says `straight_distance` m or the timeout passes."""
        start, yaw0, t0 = self.straight_from
        problem = self.guard.problem() or (f"控制迴圈停頓 {gap:.2f} 秒" if stalled else "")
        odom = (self.guard.client.state or {}).get("odom") or {}
        pos = odom.get("position")
        if not problem and (start is None or yaw0 is None or not isinstance(pos, list) or len(pos) < 2):
            problem = "沒有 odom 位置，無法量距離"
        if problem:
            self._log(f"直走中止：{problem}")
            self.guard.halt(problem)
            self.set_mode("idle", "直走中止")
            return None
        dx, dy = pos[0] - start[0], pos[1] - start[1]
        along = dx * math.cos(yaw0) + dy * math.sin(yaw0)  # progress along the starting heading
        elapsed = now - t0
        if along >= self.straight_distance or elapsed >= self.straight_timeout:
            why = (f"走完 {along:.2f} 公尺（odom）" if along >= self.straight_distance
                   else f"時間到 {elapsed:.0f} 秒，odom 量到 {along:.2f} 公尺")
            self.guard.confirm_stopped()
            self._log(f"直走結束：{why}")
            self.on_stage = not getattr(self, "going_home", False)
            self.going_home = False
            self.set_mode("idle", "直走結束")
            return None
        refused = self.guard.set_velocity(self.guard.limits.max_forward, 0.0)
        if refused:
            self._log(f"直走中止：{refused}")
            self.set_mode("idle", "直走被擋下")
            return None
        self.guard.set_head(list(ZERO_HEAD))
        reason = f"直走 {along:.2f}/{self.straight_distance:g} m · {elapsed:.0f}/{self.straight_timeout:g} s"
        self.problem = ""
        out = Decision(head=list(ZERO_HEAD), vx=self.guard.limits.max_forward,
                       desired_vx=self.guard.limits.max_forward, reason=reason, valid=True, phase="straight")
        self.last_decision = out
        return out

    def _tick_greet(self, now: float) -> Optional[Decision]:
        """Greeting and dance: a keyframe routine on the spot (head, mouth, sounds, body pose)."""
        label = self.routine.name
        problem = self.guard.head_problem()
        if problem:
            self._log(f"{label}中止：{problem}")
            self.guard.set_mouth(0.0)
            self.set_mode("idle", f"{label}中止")
            return None
        self.guard.halt()  # the body stays still for the whole routine
        head, mouth, sounds, done = self.routine.update(now)
        self.guard.set_head(list(head))
        self.guard.set_mouth(mouth)
        if self.routine.pose is not None and not done:
            self.guard.set_pose(*self.routine.pose)
        for tag in sounds:
            refused = self.guard.play(tag)
            if refused:
                self._log(refused)
        out = Decision(head=list(head), vx=0.0, desired_vx=0.0, reason=label, valid=True, phase=self.mode)
        if done:
            self.guard.set_mouth(0.0)
            self.set_mode(self.return_mode, f"{label}結束")
        self.last_decision = out
        return out

    def _state_head(self):
        head = (self.guard.client.state or {}).get("head")
        return list(head) if isinstance(head, list) and len(head) == 4 else None

    # ── turning ──────────────────────────────────────────────────────────────
    def _start_turn(self, kind: str, delta: float, now: float, head=None) -> None:
        label = "回家轉身" if kind == "home" else "對正轉身"
        self.turn = Turn(delta, odom_yaw(self.guard.client.state), now, self.turn_options, label)
        self.turn_kind = kind
        self.turn_head = list(head) if head is not None else list(ZERO_HEAD)
        self._log(f"{label}：{math.degrees(delta):+.0f}°（正值左轉）")
        if self.turn.status == FAILED:
            reason = self.turn.reason
            self.turn, self.turn_kind = None, None
            self._log(f"{label}失敗：{reason}")
            self.set_mode("idle", f"{label}失敗")

    def _end_turn(self, now: float) -> None:
        self.guard.confirm_stopped()
        kind = self.turn_kind
        self.turn, self.turn_kind = None, None
        if kind == "home" and self.scripted:
            self.going_home = True
            self.set_mode("straight", "回家：轉身完成，直走回去")
        elif kind == "home":
            self.set_mode("follow", "回家：轉身完成，跟隨主持人")
        else:
            # Same discipline as after any pulse: acknowledged stop, settle, fresh frame.
            self.cycle.start()
            self.tracker.reset(self.turn_head)
            self.last_detection_at = -math.inf
            self.last_ingested = now

    def _tick_turn(self, now: float, dt: float, problem: str) -> Decision:
        turn = self.turn
        # The head swings back to centre while the body turns, so the camera ends up on the target.
        step = self.tracker.o.head_rate * dt
        self.turn_head[2] -= max(-step, min(step, self.turn_head[2]))
        vyaw, status = 0.0, turn.status
        if problem:
            self._log(f"{turn.label}中止：{problem}")
            self.guard.halt(problem)
            self.turn, self.turn_kind = None, None
            self.set_mode("idle", f"{turn.label}中止")
            status = FAILED
        else:
            vyaw, status = turn.update(now, odom_yaw(self.guard.client.state))
            if status == TURNING:
                refused = self.guard.set_velocity(0.0, vyaw)
                if refused:
                    self._log(f"{turn.label}中止：{refused}")
                    self.turn, self.turn_kind = None, None
                    self.set_mode("idle", f"{turn.label}被擋下")
                    status, vyaw = FAILED, 0.0
            elif status == DONE:
                self._log(turn.reason)
                self._end_turn(now)
            else:
                self._log(f"{turn.label}失敗：{turn.reason}")
                self.turn, self.turn_kind = None, None
                self.set_mode("idle", f"{turn.label}失敗")
        self.guard.set_head(list(self.turn_head))
        reason = f"{turn.label}：剩 {math.degrees(turn.remaining):+.0f}°" if status == TURNING else (turn.reason or turn.label)
        if reason != self._last_reason and status != TURNING:
            self._log(f"[{self.mode}] {reason}")
        self._last_reason = reason
        out = Decision(head=list(self.turn_head), vx=0.0, desired_vx=0.0, reason=reason,
                       valid=status != FAILED, vyaw=vyaw, phase="turning")
        self.last_decision = out
        return out

    def _should_align(self, out: Decision, now: float, problem: str) -> bool:
        """Target centred in the image but the head is turned far: turn the body to face it."""
        target = self.tracker.target
        return (self.mode == "follow" and self.cycle.phase == "detecting" and not problem and out.valid
                and target is not None and abs(target.x) < 0.35
                and abs(out.head[2]) > self.align_threshold
                and self.cycle.accepts(self.last_detection_at, now)
                and self.last_detection_at > self.cycle.used)

    # ── control loop ─────────────────────────────────────────────────────────
    def readiness(self, now: float, frame: Optional[Frame]) -> str:
        source_error = getattr(self.source, "error", "")
        if source_error:
            return f"相機程式停止：{source_error}"
        if frame is None or now - frame.video_at > self.video_timeout:
            shown = "尚未收到" if frame is None else f"{now - frame.video_at:.1f} 秒"
            return f"視訊沒有新畫面（{shown}，期限 {self.video_timeout:g} 秒）"
        if (self.mode == "follow" and self.cycle.phase == "moving"
                and now - frame.video_at > self.moving_frame_timeout):
            return f"移動中 {now - frame.video_at:.1f} 秒沒有新畫面，煞停"
        return self.guard.problem()

    def tick(self) -> Optional[Decision]:
        now = self._clock()
        gap = None if self.last_tick is None else now - self.last_tick
        stalled = gap is not None and gap > self.stall_seconds
        dt = 0.1 if gap is None else min(max(gap, 0.0), 0.5)
        self.last_tick = now
        for command, source in self.commands.drain_items():
            self.handle(command, source)
        if self.recorder is not None:
            self.recorder.write(self.source.latest())  # in every mode, so idle stretches are kept too
        if self.mode == "idle":
            self.guard.halt()
            self.problem = ""
            if now <= self._deferred_dance and still(self.guard.client.state or {}):
                self._deferred_dance = -math.inf
                if self._ready_for_posture("跳舞"):
                    self.return_mode = "gaze"
                    self.set_mode("dance", "停穩了，開始跳舞")
            return None
        # Sitting and skills pass through policies/flags that would be "hard problems" for walking
        # (sit, rise, roulade, even fallen during a roll); they check what matters to them.
        if self.mode == "skill":
            self.problem = ""
            return self._tick_skill(now)
        if self.mode == "sit":
            self.problem = ""
            return self._tick_sit(now)
        hard = hard_problem(self.guard.client.state, self.guard.health, self.guard.limits,
                            self.guard.battery_low, walking=self.mode in ("follow", "home", "straight"))
        if hard:
            # A fall, protection, overheating, a low battery…: never resume by itself, even once the
            # duck is stood back up — upstream stops control here too. The operator asks again.
            self._log(f"停止並回到待命：{hard}（狀況解除後請重新下指令）")
            self.set_mode("idle", hard)
            self.problem = hard
            return None
        if self.mode in ("greet", "dance"):
            return self._tick_greet(now)
        if self.mode == "straight":
            return self._tick_straight(now, stalled, gap)
        if self.mode == "crouch":
            return self._tick_crouch(now)

        frame = self.source.latest()
        stall_problem = f"控制迴圈停頓 {gap:.2f} 秒（上限 {self.stall_seconds:g} 秒）" if stalled else ""
        if self.turn is not None:
            # Turning in place needs a healthy robot, not the camera.
            problem = self.guard.problem() or stall_problem
            self.problem = problem
            return self._tick_turn(now, dt, problem)

        problem = self.readiness(now, frame)
        if self.mode == "follow" and not problem:
            problem = stall_problem
        if problem and problem != self.problem:
            self._log(f"不可移動：{problem}")
        self.problem = problem

        if self.mode == "follow":
            if problem:
                self.guard.halt(problem)
                if self.cycle.phase not in ("stopping", "idle"):
                    self.cycle.start()  # must stop, settle and see a fresh frame before moving again
            want_frame = self.cycle.can_capture(now, frame.video_at if frame else -math.inf)
            # While a pulse runs, frames are still read — only so that losing the target brakes
            # at once (project rule). FollowCycle never lets a frame extend a pulse.
            want_frame = want_frame or self.cycle.phase == "moving"
        else:
            want_frame = True
        if frame is not None and want_frame and frame.captured_at > self.last_ingested:
            self.tracker.ingest(frame.detections, frame.captured_at)
            self.last_ingested = frame.captured_at
            self.last_detection_at = frame.captured_at

        out = self.tracker.output(now, "follow" if self.mode == "follow" else "gaze", dt)
        if self._should_align(out, now, problem):
            self.cycle.used = self.last_detection_at  # this detection is consumed by the turn
            self._start_turn("align", out.head[2], now, out.head)
            if self.turn is not None:
                return self._tick_turn(now, dt, problem)
            return None
        if self.mode == "follow":
            before = self.cycle.phase
            out = self.cycle.update(out, self.last_detection_at, now)
            if out.phase == "moving" and before != "moving":
                self._log(f"短程移動：vx 上限 {self.cycle.speed:+.2f} m/s，最長 {self.cycle.move_seconds:g} 秒")
            elif before == "moving" and out.phase != "moving":
                self._log(f"本段結束：{self.cycle.stop_reason}（峰值 {self.cycle.peak_speed:.2f} m/s）")
            if self.cycle.phase == "stopping" and not problem:
                if self.guard.confirm_stopped():
                    # Read the clock after the (possibly slow) acknowledgement, not the tick's
                    # start: otherwise the settle shrinks and frames taken while still moving
                    # would count as post-stop.
                    acked_at = self._clock()
                    self.cycle.acknowledge_stop(acked_at)
                    self.tracker.reset(list(out.head))
                    self.last_detection_at = -math.inf
                    self.last_ingested = acked_at  # nothing captured before the stop may be used
            elif self.cycle.phase == "moving" and not problem:
                refused = self.guard.set_velocity(out.vx, 0.0)
                if refused:
                    self.cycle.brake(refused)
            else:
                self.guard.halt()
        else:
            self.guard.halt()
        self.guard.set_head(list(out.head))  # frozen on loss; MotionGuard gates it on state

        self._hint_if_lost(out, now)
        if out.reason != self._last_reason:
            self._log(f"[{self.mode}] {out.reason}")
            self._last_reason = out.reason
        self.last_decision = out
        return out

    def _hint_if_lost(self, out: Decision, now: float) -> None:
        """Following or watching but no marker in sight for a while: a questioning call, then again
        every LOST_HINT_REPEAT s, so the host knows the duck is waiting for him rather than broken."""
        if out.valid:
            self.last_target_at = now
            return
        since = max(self.mode_since, self.last_target_at)
        if now - since >= LOST_HINT_AFTER and now - self.lost_hint_at >= LOST_HINT_REPEAT:
            self.lost_hint_at = now
            self._log("找不到標記：叫一聲提示（標記要在鴨子正前方約 1 公尺、正對相機）")
            self.guard.play("inquire")

    def run(self, rate_hz: float = 10.0, on_tick: Optional[Callable[[], None]] = None) -> None:
        period = 1.0 / rate_hz
        while self.running:
            started = self._clock()
            self.tick()
            if on_tick is not None:
                on_tick()
            time.sleep(max(0.0, period - (self._clock() - started)))


def make_fifo(path: Path, flag: str) -> None:
    import os
    import stat

    if path.exists() and not stat.S_ISFIFO(path.stat().st_mode):
        raise SystemExit(f"{flag} {path} 已存在但不是 FIFO")
    if not path.exists():
        os.mkfifo(path, 0o600)


def start_command_pipe(path: Path, commands: CommandQueue, say: Callable[[str], None],
                       stop_on_eof: bool = False) -> threading.Thread:
    """Read command labels from a named pipe on a daemon thread, as source "voice".

    The writer (the laptop's recogniser over SSH) may come and go: on EOF the pipe is reopened.
    Unknown lines are logged and ignored; nothing here can move the duck except through the
    same CommandQueue and gates as the keyboard.

    `stop_on_eof` (standby): the show no longer dies with the SSH session, so a writer that goes
    away — the laptop or the Wi-Fi dropped — queues a "stop" instead, as losing SSH used to.
    """
    make_fifo(path, "--command-pipe")

    def reader() -> None:
        while True:
            with open(path, "r", encoding="utf-8", errors="replace") as fifo:  # blocks until a writer
                for line in fifo:
                    label = line.strip().lower()
                    if not label:
                        continue
                    if label not in COMMANDS:
                        say(f"筆電語音：不認得的指令 {label!r}")
                        continue
                    say(f"筆電語音：{label}")
                    commands.put(label, source="voice")
            if stop_on_eof:
                say("筆電語音斷線：停下（重新連上後可繼續下指令）")
                commands.put("stop", source="keyboard")  # a link stop, not a heard word: no ack sound

    thread = threading.Thread(target=reader, name="follow-command-pipe", daemon=True)
    thread.start()
    return thread


class KeyPipe:
    """Standby (`--standby`): operator keys arrive on a named pipe instead of stdin.

    The show runs from boot with no terminal (follow/standby.sh), so the console
    (follow/console.sh, over SSH) writes the operator's lines here. Nothing is armed until a
    console sends the line "yes" — the same typed confirmation as the stdin prompt — and only
    after that do lines become commands (source "keyboard", exactly like stdin). A console that
    goes away after arming queues a "stop", as a dropped SSH session did before.
    """

    def __init__(self, path: Path, say: Callable[[str], None]):
        make_fifo(path, "--key-pipe")
        self.path = path
        self.say = say
        self.armed = threading.Event()
        self.commands: Optional[CommandQueue] = None
        self._lock = threading.Lock()

    def start(self) -> "KeyPipe":
        threading.Thread(target=self._reader, name="follow-key-pipe", daemon=True).start()
        return self

    def attach(self, commands: CommandQueue) -> None:
        with self._lock:
            self.commands = commands

    def feed(self, line: str) -> None:
        """One line from the console (also the unit-test entry point)."""
        text = line.strip().lower()
        if not self.armed.is_set():
            if text == "yes":
                self.armed.set()
                self.say("✓ 已解鎖：開始接受指令")
            else:
                self.say(f"還沒解鎖（收到 {text!r}）：扶好鴨子後在操作台輸入 yes")
            return
        if text == "yes":
            self.say("已經解鎖了")
            return
        with self._lock:
            commands = self.commands
        if commands is None:
            return
        command = parse_command(line)
        if command is None:
            self.say(f"不認得的指令：{line.strip()!r}")
        else:
            commands.put(command, source="keyboard")

    def closed(self) -> None:
        """The console's writer went away (EOF)."""
        with self._lock:
            commands = self.commands
        if self.armed.is_set() and commands is not None:
            self.say("操作台斷線：停下（重新連上 console 後可繼續）")
            commands.put("stop", source="keyboard")

    def _reader(self) -> None:
        while True:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fifo:  # blocks until a writer
                self.say("操作台已連上")
                for line in fifo:
                    self.feed(line.rstrip("\n"))
            self.closed()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Microduck follow main program")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--replay", help="JSONL detections to replay (always dry run)")
    parser.add_argument("--camera", metavar="DEVICE",
                        help="rkisp video node for follow/vision/aruco_worker.py (mediad must be stopped)")
    parser.add_argument("--vision-dir", type=Path, default=Path.home() / "microduck-vision",
                        help="upstream vision_follow install (aruco_detector.py + .venv)")
    parser.add_argument("--rotate", type=int, default=90, choices=(0, 90, 180, 270))
    parser.add_argument("--detect-hz", type=float, default=2.0,
                        help="detections per second, 0.2..5 (thermal: lower it if the CPU nears 90 °C)")
    parser.add_argument("--no-auto-exposure", dest="auto_exposure", action="store_false",
                        help="turn off the camera worker's auto-exposure (on by default with --camera: measured "
                             "2026-11-23, stopping mediad left the sensor at 159 lines / 1x, mean luma 3-11)")
    parser.add_argument("--relay", type=int, nargs="?", const=8091, metavar="PORT",
                        help="plan B: browser relay on PORT (mediad keeps running; open http://<duck>:PORT/)")
    parser.add_argument("--live", action="store_true",
                        help="actually move the robot (needs --camera or --relay, and a typed confirmation)")
    parser.add_argument("--target-id", type=int, default=0, help="ArUco marker id to follow")
    parser.add_argument("--forward", type=float, default=0.12,
                        help="pulse speed and cap, m/s (pulses ask for at most 0.25; the guard caps at ≤ 0.30)")
    parser.add_argument("--scripted", action="store_true",
                        help="no camera: f / \"Let's go\" walks straight --walk-m metres on stage, "
                             "h / \"Let's go home\" turns round and walks straight back")
    parser.add_argument("--walk-m", type=float, default=STRAIGHT_DISTANCE,
                        help="metres for the scripted / k straight walk (0.3..4.0)")
    parser.add_argument("--hot-joint", metavar="NAME:C",
                        help="one servo whose temperature reads high gets its own limit, e.g. "
                             "left_hip_roll:80 (others stay at 65 °C; at most 85)")
    parser.add_argument("--home-turn", type=float, default=180.0, metavar="DEG",
                        help="'home' first turns this many degrees in place (+ = left); 0 = no fixed "
                             "turn, just follow the host (the align turn faces them)")
    parser.add_argument("--voice", action="store_true", help="English voice commands (follow/voice/kws_listen.py)")
    parser.add_argument("--speech-dir", type=Path, default=Path.home() / "microduck-speech" / "current")
    parser.add_argument("--mic", default="hw:microducki2s3,0", help="ALSA input device for --voice (native hw node)")
    parser.add_argument("--mic-gain", type=float, default=20.0, help="digital gain for the microphone (speech ≈ -45 dBFS)")
    parser.add_argument("--kws-model", default="zh-en", choices=("gigaspeech", "zh-en"),
                        help="keyword model: upstream zh-en (default; measured best on real speakers on the duck's mic, "
                             "2026-11-24) or English GigaSpeech")
    parser.add_argument("--kws-threshold", type=float, default=0.35,
                        help="keyword threshold 0.1..0.6: lower hears more in noise, and mishears more")
    parser.add_argument("--record", metavar="JSONL", help="save every detection, replayable with --replay")
    parser.add_argument("--command-pipe", metavar="FIFO",
                        help="also take voice commands, one label per line, from this named pipe "
                             "(the laptop's speech recogniser, follow/voice/asr_bridge.py)")
    parser.add_argument("--standby", action="store_true",
                        help="started at boot with no terminal (follow/standby.sh): keys come from --key-pipe "
                             "and the typed \"yes\" too; a console or voice link that drops queues a stop")
    parser.add_argument("--key-pipe", metavar="FIFO",
                        help="operator key lines from this named pipe (follow/console.sh writes it); needs --standby")
    args = parser.parse_args(argv)
    if not 0.1 <= args.kws_threshold <= 0.6:
        parser.error("--kws-threshold 必須在 0.1..0.6")
    if not (args.home_turn == 0 or 30 <= abs(args.home_turn) <= 180):
        parser.error("--home-turn 必須是 0 或 ±30..±180 度")
    if not 0.2 <= args.detect_hz <= 5:
        print("--detect-hz 必須在 0.2..5（溫度限制）", file=sys.stderr)
        return 2

    live_source = bool(args.camera or args.relay)
    if sum(map(bool, (args.replay, args.camera, args.relay))) > 1:
        print("--replay、--camera、--relay 只能選一個。", file=sys.stderr)
        return 2
    if args.scripted and live_source:
        print("--scripted 不用相機：不要同時給 --camera / --relay。", file=sys.stderr)
        return 2
    if not 0.3 <= args.walk_m <= 4.0:
        parser.error("--walk-m 必須在 0.3..4.0 公尺")
    if args.live and not live_source and not args.scripted:
        print("--live 需要 --camera 或 --relay：不是這台鴨子即時相機的偵測結果，不能拿來驅動它。", file=sys.stderr)
        return 2
    if args.standby and not (args.scripted and args.key_pipe):
        parser.error("--standby 需要 --scripted 和 --key-pipe（開機待命不用相機，按鍵從 FIFO 來）")
    if args.key_pipe and not args.standby:
        parser.error("--key-pipe 只用在 --standby")
    hot_joint, hot_max = "", 65.0
    if args.hot_joint:
        try:
            hot_joint, hot_max = args.hot_joint.split(":")[0], float(args.hot_joint.split(":")[1])
            Limits(hot_joint=hot_joint, hot_joint_max_c=hot_max)
        except (IndexError, ValueError) as error:
            parser.error(f"--hot-joint {args.hot_joint!r}：{error}（格式 名稱:溫度，溫度 65..85）")
    limits = Limits(max_forward=args.forward, hot_joint=hot_joint, hot_joint_max_c=hot_max)
    if hot_joint:
        print(f"⚠ {hot_joint} 的溫度上限放寬到 {hot_max:g} °C（其他馬達仍是 {limits.max_motor_c:g} °C）")
    dry_run = not args.live

    # Signals first, so SIGTERM/SIGHUP at any later point still runs the finally below.
    install_signal_handlers()
    keys = None
    if args.standby:
        # No terminal: the console sends the confirmation down the key pipe. Nothing below (robotd
        # connection, guard, voice pipe) starts until it does, exactly like the stdin prompt.
        keys = KeyPipe(Path(args.key_pipe), say=lambda m: print(m, flush=True)).start()
        print(f"開機待命{'（LIVE）' if not dry_run else '（DRY RUN）'}：等操作台（follow/console.sh）輸入 yes 解鎖", flush=True)
        try:
            while not keys.armed.wait(0.5):
                pass
        except KeyboardInterrupt:
            print("已取消，什麼都沒啟動。", flush=True)
            return 1
    elif not dry_run:
        print("⚠ LIVE：鴨子會真的走路、轉身、轉頭。請確認有人在旁邊扶著、周圍淨空。")
        print("輸入 yes 後按 Enter 開始；其他任何輸入都會取消：", end="", flush=True)
        try:
            answer = sys.stdin.readline().strip()
        except KeyboardInterrupt:
            answer = ""
        if answer != "yes":
            print("\n已取消，什麼都沒啟動。")
            return 1

    printer = StatusPrinter()
    say = printer.log
    camera = voice = recorder = None
    try:
        # Subprocesses only after the confirmation, and inside the try, so they never outlive us.
        if args.camera:
            camera = ProcessDetectionSource(live=True, log=say).start(
                str(args.vision_dir / ".venv" / "bin" / "python"), str(VISION_WORKER),
                ["--vision-dir", str(args.vision_dir), "--device", args.camera, "--rotate", str(args.rotate),
                 "--hz", str(args.detect_hz)]
                + (["--auto-exposure"] if args.auto_exposure else []))
            source = camera
        elif args.relay:
            camera = ProcessDetectionSource(live=True, log=say).start(
                str(args.vision_dir / ".venv" / "bin" / "python"), str(RELAY_WORKER),
                ["--vision-dir", str(args.vision_dir), "--port", str(args.relay), "--marker-id", str(args.target_id),
                 "--hz", str(args.detect_hz)])
            say(f"瀏覽器中繼：在筆電打開 http://<鴨子IP>:{args.relay}/ → 连接摄像头 → 勾選「仅预览时启用 ArUco 识别」")
            source = camera
        else:
            source = ReplaySource.from_file(args.replay) if args.replay else NullSource()
        try:
            client = RobotdClient.connect(args.socket)
        except RobotdError as error:
            print(f"連不上 robotd：{error}", file=sys.stderr)
            return 2
        commands = CommandQueue()
        with client:
            hello = client.hello()
            banner = "DRY RUN：不會送出 robot.move / head / mouth / stop / sound" if dry_run else "LIVE"
            say(f"robotd API v{hello.get('api_version')} · {banner}")
            with MotionGuard(client, limits, dry_run=dry_run, log=say) as guard:
                guard.start()
                tracker = Tracker(default_tracker_options(args.target_id, args.forward))
                app = FollowApp(guard, source, commands, tracker=tracker, log=say,
                                home_angle=math.radians(args.home_turn), scripted=args.scripted,
                                walk_m=args.walk_m)
                if args.scripted:
                    say(f"腳本模式（不用相機）：f / Let's go = 直走 {args.walk_m:g} 公尺上台；"
                        f"h / Let's go home = 轉身 {args.home_turn:g}° 再直走回來")
                if args.record:
                    recorder = app.recorder = Recorder(args.record)
                    say(f"錄下偵測結果：{args.record}")
                if keys is not None:
                    keys.say = say
                    keys.attach(commands)
                else:
                    commands.read_lines(log=say)
                if args.command_pipe:
                    start_command_pipe(Path(args.command_pipe), commands, say, stop_on_eof=args.standby)
                    say(f"語音（筆電辨識）：從 {args.command_pipe} 接收指令")
                say("指令：f=跟隨 g=注視 w=打招呼 d=跳舞 h=回家 k=直走2公尺 s 或 Enter=停 q=結束 ｜ x=坐下 u=站起來 p=趴下 1=撿東西 2=翻滾 3/4=左/右踢（2、3、4 和從地上站起來都要連按兩次）")
                if args.voice:
                    voice = VoiceCommands(commands, log=say).start(
                        default_python(args.speech_dir),
                        ["--model", args.kws_model, "--speech-dir", str(args.speech_dir),
                         "--device", args.mic, "--gain", str(args.mic_gain),
                         "--threshold", str(args.kws_threshold)])
                    say("語音：LET'S GO / FOLLOW ME / LET'S PARTY / SHAKE IT / LET'S GO HOME / STOP / LOOK AT ME / "
                        "SAY HELLO / SIT DOWN / GET UP / LIE DOWN（鍵盤仍可用）")

                def show_status() -> None:
                    heard = voice.heard[-1] if voice is not None and voice.heard else None
                    printer.update(compose(app, time.monotonic(), voice=voice,
                                           camera=camera if live_source else None, heard=heard))

                try:
                    app.run(on_tick=show_status)
                except KeyboardInterrupt:
                    say("中斷，停止。")
                finally:
                    printer.finish()
    finally:
        if voice is not None:
            voice.close()
        if camera is not None:
            camera.close()
        if recorder is not None:
            recorder.close()
            print(f"已錄 {recorder.count} 張偵測結果：{args.record}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
