"""Posture changes and one-shot skills robotd offers (v18). Standard library only.

What robotd can do (duck-ipc-proto rev a9be46e, board build 2026-10-23):
* `robot.do {skill}` — one-shot skills: `sit_toggle` (sit if standing, stand if sitting),
  `ground_pick` (bend down and "pick"), `roulade` (forward roll, ~1 s), `kick_left`,
  `kick_right`. The board's robotd logged `kicks=false`: this duck has no kick networks, so
  robotd refuses kicks with a reason — they are offered anyway and the refusal is shown.
* `robot.pose {z, roll, pitch, active}` — standing body-pose offsets, continuous intent.
  Trained ranges are SMALL: z −0.025..+0.010 m, roll/pitch ±0.26 rad, and robotd clamps nothing
  (out-of-range values feed the policy inputs it never saw), so we clamp here. There is no
  "lie down" skill and no lying policy: "趴下" is the deepest trained crouch plus a forward lean.
  `active: false` snaps the body back to nominal at once (the prototype's B-button exit).
* `robot.enable {on, toggle[, stand_up]}` — start the policy; from limp/held it is the bring-up.
  `stand_up` only exists from 10-27 (still API v18); the board's 10-23 build rejects it as an
  unknown field, and in that build plain `{on: true}` IS the bring-up.

Nothing here ever sends robot.relax or robot.init.
"""

from __future__ import annotations

from typing import Any, Callable, Tuple

ONE_SHOT_SKILLS = ("ground_pick", "roulade", "kick_left", "kick_right")
SKILLS = ONE_SHOT_SKILLS + ("sit_toggle",)

Z_RANGE = (-0.025, 0.010)   # m
TILT_LIMIT = 0.26           # rad, roll and pitch
# 趴下: the deepest trained crouch with a forward lean. The pitch SIGN is not verified on the
# robot yet (right-handed, y left → positive pitch should be nose-down); check it held by hand.
CROUCH = {"z": -0.025, "roll": 0.0, "pitch": 0.15}

STANDING_POLICIES = ("walk", "stand")
SITTING_POLICIES = ("sit",)
# robotd is in the middle of something it will finish on its own. robot.stop does NOT cancel a
# skill (it only zeroes the twist), so while one of these is reported nothing new may be asked.
BUSY_POLICIES = ("rise", "standup", "roulade", "ground_pick", "kick_left", "kick_right")
VENDOR_FLOOR_V = 7.5  # rising draws a burst of current; below this the board may brown out mid-rise
STATE_FRESH = 1.0     # s; a posture decision on an older robot.state is refused


def still(state: Any) -> bool:
    """Standing AND not walking: policy `stand`, or `walk` with ~zero applied twist.
    A posture request right after robot.stop may otherwise land while the gait is mid-step."""
    if not isinstance(state, dict):
        return False
    safety = state.get("safety") or {}
    if not isinstance(safety, dict) or safety.get("fallen") is not False or safety.get("limp") is not False:
        return False
    policy = state.get("policy")
    if policy == "stand":
        return True
    if policy != "walk":
        return False
    applied = (state.get("move") or {}).get("applied") if isinstance(state.get("move"), dict) else None
    return (isinstance(applied, list) and len(applied) == 3
            and all(isinstance(v, (int, float)) and abs(v) < 0.02 for v in applied))


def _battery_problem(health: Any) -> str:
    volts = ((health or {}).get("battery") or {}).get("volts") if isinstance(health, dict) else None
    if not isinstance(volts, (int, float)) or isinstance(volts, bool) or volts <= VENDOR_FLOOR_V:
        return f"電池 {volts} V 不高於 {VENDOR_FLOOR_V} V：起身瞬間電流大，可能讓主機板斷電重開，先充電"
    return ""


def _evidence_problem(health: Any, health_age: Any) -> str:
    """IMU / bus / healthy evidence (as vision_follow asks before a stand-up). Battery, CPU and
    motor temperature are judged by the caller, not here."""
    from .safety import Limits, health_problem
    problem = health_problem(health, health_age, Limits(min_battery_v=VENDOR_FLOOR_V))
    if problem and not any(word in problem for word in ("電池", "CPU", "馬達")):
        return problem
    return ""


def rise_problem(state: Any, state_age: Any, health: Any, health_age: Any, failed: str = "") -> str:
    """"" when "get up from sitting" (sit_toggle) may be sent, else why not."""
    if failed:
        return failed
    if not isinstance(state, dict) or not isinstance(state_age, (int, float)) or state_age > STATE_FRESH:
        return "機器人狀態沒有更新（超過 1 秒），等收到新狀態再試"
    safety = state.get("safety") or {}
    if state.get("policy") not in SITTING_POLICIES:
        return f"不是坐著（policy={state.get('policy')}）"
    if not isinstance(safety, dict) or safety.get("fallen") is not False or safety.get("limp") is not False:
        return "坐著但倒地／卸力：扶好後連按兩次 u 從地上起身"
    return _battery_problem(health) or _evidence_problem(health, health_age)


def bring_up_problem(state: Any, state_age: Any, health: Any, health_age: Any, failed: str = "",
                     allow_protection: bool = False) -> str:
    """"" when the bring-up (robot.enable) may be offered: only from held / fallen / limp, on a
    fresh state, never while robotd is busy or the duck sits. `allow_protection` is for the
    separate operator tool stand.py only (robotd's IPC enable overrides the protection latch)."""
    if failed:
        return failed
    if not isinstance(state, dict) or not isinstance(state_age, (int, float)) or state_age > STATE_FRESH:
        return "機器人狀態沒有更新（超過 1 秒），等收到新狀態再試"
    policy = state.get("policy")
    safety = state.get("safety") if isinstance(state.get("safety"), dict) else {}
    down = safety.get("fallen") is True or safety.get("limp") is True
    if policy in BUSY_POLICIES:
        return f"robotd 正在 {policy}，等它做完，不要重複送"
    if policy in SITTING_POLICIES:
        return "坐著：用 follow.app 的 u（sit_toggle）站起來，不用起身流程"
    if policy == "protection" and not allow_protection:
        return "保護已鎖定：先查原因；真的要起身，扶好後用 python3 -m follow.stand（會問 yes）"
    if policy in STANDING_POLICIES and not down:
        return "已經站著"
    if not (policy == "held" or down or (allow_protection and policy == "protection")):
        return f"policy={policy} 不是可以起身的狀態"
    return _battery_problem(health) or _evidence_problem(health, health_age)


def clamp_pose(z: Any, roll: Any, pitch: Any) -> Tuple[float, float, float]:
    def finite(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")
    z = float(z) if finite(z) else 0.0
    roll = float(roll) if finite(roll) else 0.0
    pitch = float(pitch) if finite(pitch) else 0.0
    return (max(Z_RANGE[0], min(Z_RANGE[1], z)),
            max(-TILT_LIMIT, min(TILT_LIMIT, roll)),
            max(-TILT_LIMIT, min(TILT_LIMIT, pitch)))


def unknown_stand_up(error: Exception) -> bool:
    """robotd's serde refusal for a field it does not know: unknown field `stand_up`."""
    text = str(error)
    return "unknown field" in text and "stand_up" in text


def bring_up(client, log: Callable[[str], None] = print) -> str:
    """Ask robotd to start the policy from held / limp / fallen. "" when accepted, else why not.

    Sends exactly what upstream's 起身 button and keyboard-control's Enter send. Falls back to
    plain `{on: true}` only on the exact "unknown field stand_up" refusal of the older build.
    """
    from .robotd_client import RobotdError
    try:
        result = client.call("robot.enable", {"on": True, "toggle": False, "stand_up": True}, allow_refusal=True)
    except RobotdError as error:
        if not unknown_stand_up(error):
            return f"起身請求失敗：{error}"
        log("這版 robotd 不認得 stand_up（10/27 之前的版本）；改送它的啟用請求 robot.enable {on: true}")
        try:
            result = client.call("robot.enable", {"on": True, "toggle": False}, allow_refusal=True)
        except RobotdError as second:
            return f"啟用請求失敗：{second}"
    if not isinstance(result, dict) or result.get("accepted") is not True:
        reason = result.get("reason", result) if isinstance(result, dict) else result
        return f"robotd 沒有接受起身請求：{reason}"
    return ""
