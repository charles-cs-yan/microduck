"""Motion gate and limits for the follow module. Pure functions, no I/O.

Ported from `microduck/vision_follow/core.js` (`stateProblem`, `imuEvidenceProblem`) and
adapted to the board's v18 protocol, where `safety` is `{fallen, limp, gravity, gain}` and
has no `fall` sub-object. Every function returns "" when motion is allowed, otherwise a
human-readable reason (Traditional Chinese, shown to the operator).

Project rules encoded here (CLAUDE.md):
* forward ≤ 0.12 m/s to start, never above the 0.30 m/s operator ceiling;
* vy is never used;
* stop walking at 7.2 V (the user's floor since 2026-11-25; the vendor reference is 7.5 V);
* detection is thermally limited — refuse to move when the CPU is too hot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

OPERATOR_FORWARD_CEILING = 0.30  # m/s; customer-facing hard cap, never configurable above
MOVING_POLICIES = ("walk", "stand")  # v18 labels: walk/stand/sit/rise/roulade/kick_*/ground_pick/protection/held


@dataclass(frozen=True)
class Limits:
    max_forward: float = 0.12      # m/s
    max_backward: float = 0.10     # m/s
    max_yaw_rate: float = 0.50     # rad/s
    # V; at or below this, walking stops and the operator is told to charge. 0.1 V above the
    # vendor's 7.5 V reference because robotd's reading is smoothed (~10 s time constant) and
    # misses the sag when motors start (vendor 斷線處理指南 §1.1, §2.1).
    min_battery_v: float = 7.2
    max_cpu_c: float = 90.0        # °C; full-speed detection reaches ~95 °C and throttles to 408 MHz
    max_motor_c: float = 65.0      # °C, hottest servo case
    # One servo whose temperature reading is known to run high (left_hip_roll after the 11/24
    # fall read ~15 °C hotter than it felt) may get its own, higher limit. Off by default.
    hot_joint: str = ""
    hot_joint_max_c: float = 65.0
    max_imu_stale_run: int = 5     # consecutive repeated IMU blocks (robotd itself calls 25 "frozen")
    max_bus_errors: int = 0        # consecutive servo-bus read errors tolerated
    max_state_age: float = 0.5     # s since the last robot.state push
    max_health_age: float = 0.5    # s since the last robot.health reply
    max_mode_age: float = 3.8      # s since robot.mode said "walk" (vision_follow polls every 1.8 s)

    def __post_init__(self) -> None:
        for name in ("max_forward", "max_backward", "max_yaw_rate", "min_battery_v",
                     "max_cpu_c", "max_motor_c", "max_state_age", "max_health_age", "max_mode_age"):
            value = getattr(self, name)
            if not _finite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.max_forward > OPERATOR_FORWARD_CEILING:
            raise ValueError(f"max_forward {self.max_forward} exceeds the {OPERATOR_FORWARD_CEILING} m/s ceiling")
        if self.max_backward > OPERATOR_FORWARD_CEILING:
            raise ValueError("max_backward exceeds the operator ceiling")
        if self.max_yaw_rate > 1.0:
            raise ValueError("max_yaw_rate above 1.0 rad/s is outside this demo's envelope")
        if self.min_battery_v < 7.2:
            raise ValueError("min_battery_v may not be set below 7.2 V")
        if not _finite(self.hot_joint_max_c) or not self.max_motor_c <= self.hot_joint_max_c <= 85.0:
            raise ValueError("hot_joint_max_c must be between max_motor_c and 85 °C")


def motor_limit(motors: Dict[str, Any], limits: "Limits") -> float:
    """The limit for the hottest servo: its own when it is the named hot joint, else max_motor_c."""
    if limits.hot_joint and motors.get("hottest") == limits.hot_joint:
        return limits.hot_joint_max_c
    return limits.max_motor_c


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clamp_command(vx: Any, vyaw: Any, limits: Limits) -> Tuple[float, float, float]:
    """Return a safe (vx, vy, vyaw). Non-finite input becomes 0; vy is always 0."""
    vx = float(vx) if _finite(vx) else 0.0
    vyaw = float(vyaw) if _finite(vyaw) else 0.0
    vx = max(-limits.max_backward, min(limits.max_forward, vx))
    vyaw = max(-limits.max_yaw_rate, min(limits.max_yaw_rate, vyaw))
    return vx, 0.0, vyaw


# [neck_pitch, head_pitch, head_yaw, head_roll] in radians. Yaw/pitch match the tracker's own
# limits (vision_follow core.js yawLimit .60 / pitchLimit .35); neck and roll only hold posture.
HEAD_LIMITS = (0.30, 0.35, 0.60, 0.30)
HEAD_KEYS = ("neck_pitch", "head_pitch", "head_yaw", "head_roll")


def clamp_head(head: Any) -> Optional[Tuple[float, float, float, float]]:
    """Clamp a 4-vector head target; None if it is not four finite numbers."""
    if not isinstance(head, (list, tuple)) or len(head) != 4 or not all(_finite(v) for v in head):
        return None
    return tuple(max(-lim, min(lim, float(v))) for v, lim in zip(head, HEAD_LIMITS))  # type: ignore[return-value]


def mode_problem(mode: Optional[str], age: Optional[float], limits: Limits) -> str:
    if mode is None or not _finite(age) or age < 0 or age > limits.max_mode_age:
        return "等待 robot.mode 確認 walk 模式"
    if mode != "walk":
        return f"機器人不在 walk 模式（mode={mode}）"
    return ""


def battery_problem(health: Optional[Dict[str, Any]], limits: Limits) -> str:
    battery = (health or {}).get("battery")
    if not isinstance(battery, dict) or not _finite(battery.get("volts")):
        return "沒有電池電壓讀數"
    volts = battery["volts"]
    if volts <= limits.min_battery_v:
        return f"電池 {volts:.2f} V，已到 {limits.min_battery_v:.1f} V 停止線，請充電"
    return ""


def health_problem(health: Optional[Dict[str, Any]], age: Optional[float], limits: Limits) -> str:
    """Checks on a `robot.health` reply. Mirrors imuEvidenceProblem plus battery/thermal."""
    if health is None or not _finite(age) or age < 0 or age > limits.max_health_age:
        shown = f"{age * 1000:.0f} ms" if _finite(age) else "未知"
        return f"缺少 {limits.max_health_age:g} 秒內的健康檢查（年齡={shown}）"
    if health.get("healthy") is not True:
        return f"機器人健康檢查未通過：{health.get('reason') or '原因未知'}"
    imu = health.get("imu")
    if not isinstance(imu, dict) or imu.get("ready") is not True:
        return "IMU 未就緒或姿態資料過期"
    run = imu.get("consecutive_stale_blocks")
    if not isinstance(run, int) or isinstance(run, bool) or run < 0:
        return "健康回覆缺少有效的 IMU 連續重複計數"
    if run >= limits.max_imu_stale_run:
        return f"IMU 連續重複取樣 {run} 次"
    bus = health.get("bus") or {}
    errors = bus.get("consecutive_errors", 0) if isinstance(bus, dict) else None
    if not isinstance(errors, int) or errors > limits.max_bus_errors:
        return f"舵機匯流排讀取失敗（連續 {errors} 次）"
    problem = battery_problem(health, limits)
    if problem:
        return problem
    cpu = health.get("cpu_temp_c")
    if _finite(cpu) and cpu >= limits.max_cpu_c:
        return f"CPU {cpu:.0f} °C，超過 {limits.max_cpu_c:.0f} °C"
    motors = health.get("motors")
    if isinstance(motors, dict) and _finite(motors.get("max_c")) and motors["max_c"] >= motor_limit(motors, limits):
        return f"馬達 {motors.get('hottest', '?')} {motors['max_c']:.0f} °C，超過 {motor_limit(motors, limits):.0f} °C"
    return ""


def state_problem(state: Optional[Dict[str, Any]], age: Optional[float], limits: Limits) -> str:
    """Checks on the latest `robot.state` push. Mirrors stateProblem (v18 fields)."""
    if state is None:
        return "等待機器人狀態"
    if not _finite(age) or age < 0 or age > limits.max_state_age:
        shown = f"{age * 1000:.0f} ms" if _finite(age) else "未知"
        return f"機器人狀態過期（年齡={shown}）"
    policy = state.get("policy")
    if policy == "protection":
        return "保護已鎖定（policy=protection）"
    if policy not in MOVING_POLICIES:
        return f"目前策略={policy or '未知'}，等待站穩的 walk/stand"
    safety = state.get("safety")
    if not isinstance(safety, dict) or safety.get("fallen") is not False or safety.get("limp") is not False:
        return "倒地、卸力或安全狀態未知"
    head = state.get("head")
    if not isinstance(head, list) or len(head) != 4 or not all(_finite(v) for v in head):
        return "頭部狀態無效"
    return ""


def hard_problem(state: Optional[Dict[str, Any]], health: Optional[Dict[str, Any]], limits: Limits,
                 battery_low: str = "", walking: bool = True) -> str:
    """Conditions after which nothing may resume by itself: the operator has to ask again.

    Upstream vision_follow stops control on any of these. Staleness (missing/old state or
    health, a stalled loop, no video) is deliberately NOT here — those clear and resume through
    the normal stop → settle → fresh-frame cycle. `walking=False` (gaze, greet) ignores the
    battery, which only stops walking.
    """
    if isinstance(state, dict):
        policy = state.get("policy")
        safety = state.get("safety")
        if isinstance(safety, dict) and (safety.get("fallen") is True or safety.get("limp") is True):
            return "倒地或卸力"
        if policy == "held":
            return "策略是 held（行走策略沒在控制）；扶好後先執行 python3 -m follow.stand"
        if policy is not None and policy not in MOVING_POLICIES:
            return f"策略變成 {policy}"
    if isinstance(health, dict):
        if health.get("healthy") is False:
            return f"健康檢查未通過：{health.get('reason') or '原因未知'}"
        cpu = health.get("cpu_temp_c")
        if _finite(cpu) and cpu >= limits.max_cpu_c:
            return f"CPU {cpu:.0f} °C 過熱"
        motors = health.get("motors")
        if isinstance(motors, dict) and _finite(motors.get("max_c")) and motors["max_c"] >= motor_limit(motors, limits):
            return f"馬達 {motors.get('hottest', '?')} {motors['max_c']:.0f} °C 過熱"
    if walking and battery_low:
        return battery_low
    return ""


def motion_problem(state: Optional[Dict[str, Any]], state_age: Optional[float],
                   health: Optional[Dict[str, Any]], health_age: Optional[float],
                   limits: Limits, mode: Optional[str] = "walk", mode_age: Optional[float] = 0.0) -> str:
    """The single gate: "" means robot.move with non-zero velocity may be sent.

    mode/mode_age default to "known walk" so callers that do not poll robot.mode (probe, old
    tests) keep their behaviour; MotionGuard always passes the real values.
    """
    return (state_problem(state, state_age, limits) or health_problem(health, health_age, limits)
            or mode_problem(mode, mode_age, limits))
