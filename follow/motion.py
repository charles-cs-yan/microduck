"""Velocity and head sender with a guaranteed stop.

`MotionGuard` is the only place in `follow/` that sends `robot.move` or `robot.head`. It:

* clamps every command through `safety.clamp_command` / `clamp_head` (vy is always 0);
* re-sends the current intents at `rate_hz` (robotd's deadman is 500 ms) while the gate allows;
* drops an intent if the caller stops refreshing it for `intent_timeout` (a client-side
  deadman in front of robotd's own);
* on any gate failure clears the velocity intent and sends an acknowledged `robot.stop` — the
  caller has to ask again, so a transient fault never resumes motion by itself;
* latches a low battery for the rest of the run;
* sends `robot.stop` on close, including when the `with` body raised.

With `dry_run=True` it evaluates the same gate against the real robot but sends nothing that
moves a motor (no robot.move / robot.head / robot.stop); `last_command` shows what it would send.

Stopping must leave the duck standing: it never sends robot.relax or robot.init. Posture changes
(robot.do skills, robot.pose crouch, robot.enable bring-up) are sent ONLY when the app's operator
asks for them (do_skill / set_pose / bring_up), each after an acknowledged stop and its own gate.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .posture import SITTING_POLICIES, SKILLS, VENDOR_FLOOR_V, bring_up, clamp_pose
from .robotd_client import RobotdClient, RobotdError
from .safety import (HEAD_KEYS, Limits, battery_problem, clamp_command, clamp_head,
                     motion_problem, state_problem)

Log = Callable[[str], None]
ZERO = (0.0, 0.0, 0.0)


class MotionGuard:
    def __init__(self, client: RobotdClient, limits: Limits = Limits(), *,
                 rate_hz: float = 10.0, health_hz: float = 4.0, mode_period: float = 1.8,
                 intent_timeout: float = 0.4, dry_run: bool = False,
                 clock: Callable[[], float] = time.monotonic, log: Log = print):
        if not 2.0 <= rate_hz <= 50.0:
            raise ValueError("rate_hz must be 2..50 so robotd's 500 ms deadman never fires mid-move")
        if not 0.1 <= intent_timeout <= 1.0:
            raise ValueError("intent_timeout must be 0.1..1.0 s")
        self.client = client
        self.limits = limits
        self.rate_hz = rate_hz
        self.health_hz = health_hz
        self.mode_period = mode_period
        self.intent_timeout = intent_timeout
        self.dry_run = dry_run
        self._clock = clock
        self._log_raw = log
        # Re-entrant: tick() sends robot.move while holding it, and a failed send calls halt().
        self._lock = threading.RLock()
        self.failed = ""  # latched by an unexpected exception in a worker thread
        self._intent: Tuple[float, float, float] = ZERO
        self._intent_at = -float("inf")
        self._head: Optional[Tuple[float, float, float, float]] = None
        self._head_at = -float("inf")
        self._mouth: Optional[float] = None
        self._mouth_at = -float("inf")
        self._mouth_used = False
        self._pose: Optional[Tuple[float, float, float]] = None
        self._pose_at = -float("inf")
        self._pose_used = False
        self._pose_active = False  # an active:true went out and no active:false since
        self.moving = False
        self.health: Optional[Dict[str, Any]] = None
        self.health_at: Optional[float] = None
        self.mode: Optional[str] = None
        self.mode_at: Optional[float] = None
        self._mode_polled = -float("inf")
        self.battery_low = ""
        self.last_problem = ""
        self.last_command: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._threads: list = []
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, *, threads: bool = True, subscribe_hz: int = 10) -> "MotionGuard":
        self.client.subscribe(subscribe_hz)
        self.poll_health()
        if threads:
            for target, name in ((self._health_loop, "follow-health"), (self._send_loop, "follow-sender")):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads.append(thread)
        return self

    def close(self) -> None:
        """Stop sending and leave the duck standing. Idempotent, never raises."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        with self._lock:
            self._intent = ZERO
            self._head = None
            self._mouth = None
            self._pose = None
        for thread in self._threads:
            thread.join(timeout=1.0)
        self.moving = False
        if self.dry_run:
            return
        if self._mouth_used:  # leave the beak shut, as keyboard-control does on exit
            try:
                self.client.notify("robot.mouth", {"open": 0.0})
            except RobotdError:
                pass
        if self._pose_used:  # leave the body at its nominal height, as keyboard-control does
            try:
                self.client.notify("robot.pose", {"z": 0.0, "roll": 0.0, "pitch": 0.0, "active": False})
            except RobotdError:
                pass
        if not self.client.stop():
            try:  # fall back to a zero intent; robotd's deadman is the last line after that
                self.client.notify("robot.move", {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
            except RobotdError:
                pass
            self._log("警告：結束時 robot.stop 沒有得到確認，靠 robotd deadman（500 ms）停下")

    def __enter__(self) -> "MotionGuard":
        return self

    def __exit__(self, *exc: Any) -> None:
        # A second Ctrl-C must not abort the stop on its way out (keyboard-control does the same).
        previous = None
        if threading.current_thread() is threading.main_thread():
            try:
                previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
            except (ValueError, OSError):
                previous = None
        try:
            self.close()
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _log(self, message: str) -> None:
        try:
            self._log_raw(message)
        except Exception:  # a broken stdout must never take the stop path down with it
            pass

    # ── gate ─────────────────────────────────────────────────────────────────
    def _age(self, at: Optional[float]) -> Optional[float]:
        return None if at is None else self._clock() - at

    def problem(self) -> str:
        if self.failed:
            return self.failed
        return motion_problem(self.client.state, self.client.state_age(), self.health,
                              self._age(self.health_at), self.limits, self.mode, self._age(self.mode_at))

    def head_problem(self) -> str:
        # The head may keep tracking with a low battery (only walking stops), but never while
        # fallen, limp, rising or with stale state.
        if self.failed:
            return self.failed
        return state_problem(self.client.state, self.client.state_age(), self.limits)

    def _fail(self, where: str, error: BaseException) -> None:
        """An unexpected exception in a worker thread: stop, latch, keep the thread alive."""
        with self._lock:
            self._intent = ZERO
            self._intent_at = -float("inf")
            self._head = None
            self._mouth = None
        if not self.failed:
            self.failed = f"內部錯誤（{where}）：{error!r}；請重新啟動程式"
            self._log("✗ " + self.failed)
        if not self.dry_run:
            try:
                if self.client.stop():
                    self.moving = False
            except Exception:
                pass

    # ── caller API ───────────────────────────────────────────────────────────
    def set_velocity(self, vx: float, vyaw: float = 0.0) -> str:
        """Request a velocity. Returns "" if accepted, else the reason it was refused.

        Must be refreshed faster than `intent_timeout`, otherwise the guard stops the duck.
        """
        if self._closed:
            return "MotionGuard 已關閉"
        command = clamp_command(vx, vyaw, self.limits)
        if command == ZERO:
            self.halt()
            return ""
        reason = self.battery_low or self.problem()
        if reason:
            self.halt(reason)
            return reason
        with self._lock:
            self._intent = command
            self._intent_at = self._clock()
        return ""

    def set_head(self, head: Any) -> str:
        """Request a head target [neck_pitch, head_pitch, head_yaw, head_roll] (rad)."""
        if self._closed:
            return "MotionGuard 已關閉"
        clamped = clamp_head(head)
        if clamped is None:
            return "頭部目標無效"
        reason = self.head_problem()
        if reason:
            with self._lock:
                self._head = None
            return reason
        with self._lock:
            self._head = clamped
            self._head_at = self._clock()
        return ""

    def set_mouth(self, opening: Any) -> str:
        """Request a mouth opening 0..1 (a servo, so gated like the head)."""
        if self._closed:
            return "MotionGuard 已關閉"
        if not isinstance(opening, (int, float)) or isinstance(opening, bool) or opening != opening:
            return "嘴巴目標無效"
        reason = self.head_problem()
        with self._lock:
            self._mouth = None if reason else max(0.0, min(1.0, float(opening)))
            self._mouth_at = self._clock()
        return reason

    # ── posture (follow.posture explains what robotd offers) ─────────────────
    def set_pose(self, z: Any, roll: Any = 0.0, pitch: Any = 0.0) -> str:
        """Hold a standing body pose (crouch / lean), clamped to the trained ranges. Continuous:
        refresh faster than intent_timeout. Only while standing — the gate is the head's."""
        if self._closed:
            return "MotionGuard 已關閉"
        reason = self.head_problem()
        with self._lock:
            self._pose = None if reason else clamp_pose(z, roll, pitch)
            self._pose_at = self._clock()
        return reason

    def release_pose(self) -> None:
        """Back to the nominal body pose now (robot.pose active:false), if a pose was held.

        Under the lock, and tick() re-checks under the same lock before each active:true, so a
        release can never be followed on the wire by a stale active:true. That matters: robotd's
        pose has NO deadman — a pose left active keeps the duck crouched (and its twist zeroed)."""
        with self._lock:
            had = self._pose is not None or self._pose_active
            self._pose = None
            self._pose_active = False
            if had and not self.dry_run and not self._closed:
                try:
                    self.client.notify("robot.pose", {"z": 0.0, "roll": 0.0, "pitch": 0.0, "active": False})
                except RobotdError:
                    pass
        self.last_command = {**self.last_command, "pose": None}

    def do_skill(self, skill: str) -> str:
        """One-shot robot.do. "" when robotd accepted it, else why not. Stops first; only from a
        standing, healthy robot (a dynamic skill on a weak battery or a hot CPU is refused)."""
        if skill not in SKILLS:
            return f"不支援的技能 {skill!r}"
        if self._closed:
            return "MotionGuard 已關閉"
        if skill == "sit_toggle":
            # sitting down is always allowed from a standing robot (it lowers the load);
            # standing back up from "sit" is checked by the caller (battery floor)
            policy = (self.client.state or {}).get("policy")
            reason = "" if policy in SITTING_POLICIES else self.head_problem()
        else:
            reason = self.battery_low or self.problem()
        if reason:
            return reason
        if not self.confirm_stopped():
            return "robot.stop 沒有得到確認，不送技能"
        self.last_command = {**self.last_command, "skill": skill}
        if self.dry_run:
            return ""
        try:
            result = self.client.call("robot.do", {"skill": skill}, timeout=2.0, allow_refusal=True)
        except RobotdError as error:
            return f"robot.do {skill} 失敗：{error}"
        if isinstance(result, dict) and result.get("accepted") is False:
            return f"robotd 拒絕 {skill}：{result.get('reason')}"
        return ""

    def bring_up(self) -> str:
        """Start the policy from held / limp / fallen (posture.bring_up). "" when accepted."""
        if self._closed:
            return "MotionGuard 已關閉"
        volts = ((self.health or {}).get("battery") or {}).get("volts")
        if not isinstance(volts, (int, float)) or volts <= VENDOR_FLOOR_V:
            return f"電池 {volts} V 不高於 {VENDOR_FLOOR_V} V：起身瞬間電流大，可能讓主機板斷電重開，先充電"
        if self.failed:
            return self.failed
        if not self.confirm_stopped():
            return "robot.stop 沒有得到確認，不送起身請求"
        self.last_command = {**self.last_command, "enable": True}
        if self.dry_run:
            return ""
        return bring_up(self.client, log=self._log)

    def play(self, tag: str) -> str:
        """Play a voice-bank sound. Speaker only; still skipped in a dry run."""
        self.last_command = {**self.last_command, "sound": tag}
        if self.dry_run or self._closed:
            return ""
        try:
            result = self.client.call("robot.sound", {"tag": tag}, timeout=1.0, allow_refusal=True)
        except RobotdError as error:
            return f"robot.sound 失敗：{error}"
        if result.get("accepted") is False:
            return f"robot.sound 被拒：{result.get('reason')}"
        return ""

    def halt(self, reason: str = "") -> None:
        """Zero the velocity intent now; the next tick sends an acknowledged robot.stop if moving."""
        with self._lock:
            self._intent = ZERO
            self._intent_at = -float("inf")
        if reason and reason != self.last_problem:
            self._log(f"停止：{reason}")
        if reason:
            self.last_problem = reason

    def confirm_stopped(self) -> bool:
        """Zero the intent and get robotd to acknowledge a stop *now* (FollowCycle's zero ack)."""
        self.halt()
        if self.dry_run:
            self.moving = False
            self.last_command = {**self.last_command, "move": ZERO}
            return True
        acknowledged = self.client.stop()
        if acknowledged:
            self.moving = False
        return acknowledged

    # ── periodic work (threads call these; tests call them directly) ────────
    def poll_health(self) -> None:
        try:
            health = self.client.health(timeout=0.5)
            if not isinstance(health, dict):
                raise RobotdError(f"robot.health 回覆格式不對：{type(health).__name__}")
        except RobotdError as error:
            self.health, self.health_at = None, None
            self.last_problem = f"健康查詢失敗：{error}"
        else:
            self.health, self.health_at = health, self._clock()
            low = battery_problem(health, self.limits)
            if low and not self.battery_low and "沒有電池" not in low:
                self.battery_low = low
                self._log(f"⚠ {low}。本次執行不再行走。")
        now = self._clock()
        if now - self._mode_polled >= self.mode_period:
            self._mode_polled = now
            try:
                self.mode, self.mode_at = self.client.mode(timeout=0.5), self._clock()
            except RobotdError:
                self.mode, self.mode_at = None, None

    def tick(self) -> Tuple[float, float, float]:
        """One sender step. Returns the velocity actually sent (zeros when none)."""
        now = self._clock()
        with self._lock:
            intent, intent_at = self._intent, self._intent_at
            head, head_at = self._head, self._head_at
            mouth, mouth_at = self._mouth, self._mouth_at
            pose, pose_at = self._pose, self._pose_at
        if pose is not None and (now - pose_at > self.intent_timeout or self.head_problem()):
            # Not refreshed, or no longer standing: actively release — robotd would hold it forever.
            self.release_pose()
            pose = None
        if pose is not None:
            with self._lock:
                if self._pose == pose:  # a release since the copy above wins
                    self._pose_used = True
                    self._pose_active = True
                    z, roll, pitch = pose
                    self._send("robot.pose", {"z": z, "roll": roll, "pitch": pitch, "active": True}, "pose", pose)
        if mouth is not None and (now - mouth_at > self.intent_timeout or self.head_problem()):
            with self._lock:
                self._mouth = None
            mouth = None
        if mouth is not None:
            self._mouth_used = True
            self._send("robot.mouth", {"open": mouth}, "mouth", mouth)
        if intent != ZERO and now - intent_at > self.intent_timeout:
            self.halt("呼叫端停止更新速度")
            intent = ZERO
        if intent != ZERO:
            reason = self.battery_low or self.problem()
            if reason:
                self.halt(reason)
                intent = ZERO
        if head is not None and (now - head_at > self.intent_timeout or self.head_problem()):
            with self._lock:
                self._head = None
            head = None
        if head is not None:
            self._send("robot.head", dict(zip(HEAD_KEYS, head)), "head", head)
        if intent != ZERO:
            vx, vy, vyaw = intent
            # Re-check and send under the lock: a halt()/confirm_stopped() that ran after the copy
            # above must win. Otherwise this move could land after an acknowledged robot.stop,
            # cancel it inside robotd, and leave both sides believing the duck had stopped.
            with self._lock:
                if self._intent == intent:
                    if not self._send("robot.move", {"vx": vx, "vy": vy, "vyaw": vyaw}, "move", intent):
                        self.moving = False
                        return ZERO
                    self.moving = True
                    return intent
        if self.moving:
            if self.dry_run:
                self.last_command = {**self.last_command, "move": ZERO}
            elif not self.client.stop():
                self._log("警告：robot.stop 沒有得到確認，靠 robotd deadman 停下")
            self.moving = False
        return ZERO

    def _send(self, method: str, params: Dict[str, float], key: str, value: Any) -> bool:
        self.last_command = {**self.last_command, key: value}
        if self.dry_run:
            return True
        try:
            self.client.notify(method, params)
        except RobotdError as error:
            self.halt(f"送出 {method} 失敗：{error}")
            return False
        return True

    def poll_health_safely(self) -> None:
        try:
            self.poll_health()
        except Exception as error:  # noqa: BLE001 - a worker thread must not die silently
            self._fail("health", error)

    def tick_safely(self) -> None:
        try:
            self.tick()
        except Exception as error:  # noqa: BLE001 - a worker thread must not die silently
            self._fail("sender", error)

    def _health_loop(self) -> None:
        period = 1.0 / self.health_hz
        while not self._stop.wait(period):
            self.poll_health_safely()

    def _send_loop(self) -> None:
        period = 1.0 / self.rate_hz
        while not self._stop.wait(period):
            self.tick_safely()


def install_signal_handlers() -> None:
    """Turn SIGTERM/SIGHUP into KeyboardInterrupt so `with MotionGuard(...)` still stops.

    Call from the main thread. Without this, `systemctl stop` or a closed SSH session would
    kill the process before `close()` sends robot.stop (robotd's deadman would still stop it).
    """
    def raise_interrupt(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), raise_interrupt)
