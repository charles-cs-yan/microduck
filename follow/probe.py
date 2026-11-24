"""Read-only probe: watch robotd for a while and report whether the follow gate would allow motion.

Sends only hello / robot.subscribe / robot.health / robot.mode. Never robot.move, robot.stop,
robot.enable or anything else that changes motors or posture, so it is safe to run unattended.

    python3 -m follow.probe --seconds 30
"""

from __future__ import annotations

import argparse
import collections
import sys
import time

from .robotd_client import DEFAULT_SOCKET, RobotdClient, RobotdError
from .safety import Limits, motion_problem


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--health-hz", type=float, default=4.0)
    args = parser.parse_args(argv)
    limits = Limits()

    try:
        client = RobotdClient.connect(args.socket)
    except RobotdError as error:
        print(f"連不上 robotd：{error}", file=sys.stderr)
        return 2
    with client:
        hello = client.hello()
        print(f"robotd API v{hello.get('api_version')} · daemon {hello.get('daemon_version')} · rev {hello.get('revision')}")
        print(f"mode = {client.mode()}")
        client.subscribe(10)

        reasons = collections.Counter()
        samples = 0
        stale_runs, bus_errors, volts, cpu = [], [], [], []
        last_reason = None
        end = time.monotonic() + args.seconds
        period = 1.0 / args.health_hz
        while time.monotonic() < end:
            try:
                health = client.health(timeout=0.5)
                health_at = time.monotonic()
            except RobotdError as error:
                health, health_at = None, None
                print(f"health 失敗：{error}")
            now = time.monotonic()
            reason = motion_problem(client.state, client.state_age(), health,
                                    None if health_at is None else now - health_at, limits)
            samples += 1
            reasons[reason or "可以移動"] += 1
            if reason != last_reason:
                print(f"[{args.seconds - (end - now):6.1f}s] {'可以移動' if not reason else '不可移動：' + reason}")
                last_reason = reason
            if health:
                imu = health.get("imu") or {}
                stale_runs.append(imu.get("consecutive_stale_blocks", -1))
                bus_errors.append((health.get("bus") or {}).get("consecutive_errors", -1))
                if (health.get("battery") or {}).get("volts") is not None:
                    volts.append(health["battery"]["volts"])
                if health.get("cpu_temp_c") is not None:
                    cpu.append(health["cpu_temp_c"])
            time.sleep(period)

    print("\n── 摘要 ──")
    print(f"取樣 {samples} 次")
    for reason, count in reasons.most_common():
        print(f"  {count:4d} × {reason}")
    if volts:
        print(f"電池 {min(volts):.2f}–{max(volts):.2f} V（停止線 {limits.min_battery_v} V）")
    if cpu:
        print(f"CPU {min(cpu):.0f}–{max(cpu):.0f} °C")
    if stale_runs:
        print(f"IMU 連續重複：最大 {max(stale_runs)}，≥{limits.max_imu_stale_run} 的比例 "
              f"{sum(r >= limits.max_imu_stale_run for r in stale_runs) / len(stale_runs):.0%}")
    if bus_errors:
        print(f"匯流排錯誤：{sum(e > 0 for e in bus_errors)}/{len(bus_errors)} 次取樣不為 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
