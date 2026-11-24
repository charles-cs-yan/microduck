"""Operator tool: bring the duck up from held / a fall (policy not running). CHANGES POSTURE.

    python3 -m follow.stand

Sends, only after the operator types "yes", exactly what upstream vision_follow's "起身"
button and keyboard-control's Enter send — robot.stop, then robot.enable {on: true, toggle:
false, stand_up: true} (plain {on: true} on the board's older robotd) — without restarting
robotd and without ever relaxing: unlike keyboard-control it never collapses the duck.
(follow.app's `u` uses the same request and the same checks, posture.bring_up_problem.)

Before asking, it refuses when: robot state is stale or missing, IMU not ready / repeating,
robotd is busy (rise / standup / a skill), the duck SITS (use follow.app's `u`, sit_toggle:
a stand_up from sitting would skip the sit-stand network on robotd ≥ 10-27), or the battery
is at or below the vendor's 7.5 V reference (rising can brown the board out).
Afterwards it watches the state for --timeout seconds and reports the outcome. If the rise
hangs it only tells the operator what to do; it does not relax the motors itself.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable, Dict, Optional

from .posture import STANDING_POLICIES as STANDING
from .posture import VENDOR_FLOOR_V, bring_up, bring_up_problem, unknown_stand_up  # noqa: F401
from .robotd_client import DEFAULT_SOCKET, RobotdClient, RobotdError

RISING = ("standup", "rise")


def standing(state: Optional[Dict[str, Any]]) -> bool:
    safety = (state or {}).get("safety") or {}
    return (state or {}).get("policy") in STANDING and safety.get("fallen") is False and safety.get("limp") is False


def precheck(state: Optional[Dict[str, Any]], state_age: Optional[float],
             health: Optional[Dict[str, Any]], health_age: float) -> str:
    """"" when a stand-up request may be offered, else the reason not to.

    Same rules as follow.app's `u` (posture.bring_up_problem): only from held / fallen / limp,
    refused while robotd is busy (rise, standup, a skill) or the duck sits (use sit_toggle —
    on robotd ≥ 10-27 a stand_up from sitting would skip the sit-stand network). As a deliberate,
    yes-confirmed operator tool it may still try from robotd's protection latch, like upstream."""
    return bring_up_problem(state, state_age, health, health_age, allow_protection=True)


def describe(state: Optional[Dict[str, Any]], health: Optional[Dict[str, Any]]) -> str:
    safety = (state or {}).get("safety") or {}
    battery = (health or {}).get("battery") or {}
    return (f"policy={(state or {}).get('policy')} fallen={safety.get('fallen')} limp={safety.get('limp')} "
            f"battery={battery.get('volts')} V cpu={(health or {}).get('cpu_temp_c')} °C")


def run(client: RobotdClient, *, confirm: Callable[[str], str] = input, timeout: float = 20.0,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print) -> int:
    client.hello()
    client.subscribe(10)
    deadline = clock() + 2.0
    while client.state is None and clock() < deadline:
        sleep(0.05)
    try:
        health, health_at = client.health(), clock()
    except RobotdError as error:
        log(f"健康查詢失敗：{error}")
        return 2
    log("目前：" + describe(client.state, health))
    if standing(client.state):
        log("已經站著，不需要起身。")
        return 0
    reason = precheck(client.state, client.state_age(), health, clock() - health_at)
    if reason:
        log(f"不送起身請求：{reason}")
        return 2
    answer = confirm("⚠ 鴨子會站起來。扶好它、手避開關節，輸入 yes 後按 Enter：")
    if answer.strip() != "yes":
        log("已取消，什麼都沒送。")
        return 1
    client.stop()
    # Shared with follow.app's "u" command (posture.bring_up): stand_up first; on the board's
    # 10-23 robotd (no stand_up field) it switches to plain {on: true}, which IS the bring-up there.
    refused = bring_up(client, log=log)
    if refused:
        log(refused + "（不會改用其他請求重試）")
        return 2
    log("起身請求已被接受，觀察中……")
    started, last_policy, saw_rising = clock(), None, False
    while clock() - started < timeout:
        state = client.state
        policy = (state or {}).get("policy")
        if policy != last_policy:
            log(f"  {clock() - started:4.1f}s policy={policy}")
            last_policy = policy
        saw_rising |= policy in RISING
        if standing(state) and (saw_rising or clock() - started > 1.0):
            log("✓ 站起來了。follow 可以開始。")
            return 0
        if policy == "protection" and (saw_rising or clock() - started > 1.0):
            log("✗ robotd 進入保護（protection）：起身失敗。先扶住，檢查電池與舵機，"
                "必要時人工執行 robotctl robot relax --yes 讓它卸力（會倒下）。")
            return 3
        sleep(0.1)
    log(f"✗ {timeout:g} 秒內沒有站穩（最後 policy={last_policy}）。扶住它；"
        "卡在起身中時，可人工執行 robotctl robot relax --yes 卸力（會倒下），本工具不會自己卸力。")
    return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        client = RobotdClient.connect(args.socket)
    except RobotdError as error:
        print(f"連不上 robotd：{error}", file=sys.stderr)
        return 2
    with client:
        try:
            return run(client, timeout=args.timeout)
        except KeyboardInterrupt:
            print("\n中斷。已送出的請求不會撤回；扶住鴨子。")
            return 130


if __name__ == "__main__":
    sys.exit(main())
