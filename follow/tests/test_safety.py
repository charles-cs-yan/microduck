import math
import unittest

from follow.safety import Limits, clamp_command, health_problem, motion_problem, state_problem
from follow.tests.fake_robotd import healthy_health, standing_state

L = Limits()


class LimitsTest(unittest.TestCase):
    def test_defaults_match_project_rules(self):
        self.assertEqual(L.max_forward, 0.12)
        self.assertEqual(L.min_battery_v, 7.2)

    def test_cannot_exceed_operator_ceiling(self):
        with self.assertRaises(ValueError):
            Limits(max_forward=0.31)
        Limits(max_forward=0.30)

    def test_cannot_lower_battery_floor(self):
        with self.assertRaises(ValueError):
            Limits(min_battery_v=7.0)
        with self.assertRaises(ValueError):
            Limits(min_battery_v=7.19)
        Limits(min_battery_v=7.2)  # the user's floor (vendor: 7.5) is the lowest allowed setting

    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            Limits(max_yaw_rate=float("nan"))


class ClampTest(unittest.TestCase):
    def test_vy_always_zero_and_limits(self):
        self.assertEqual(clamp_command(1.0, 5.0, L), (0.12, 0.0, 0.5))
        self.assertEqual(clamp_command(-1.0, -5.0, L), (-0.10, 0.0, -0.5))
        self.assertEqual(clamp_command(0.05, 0.1, L), (0.05, 0.0, 0.1))

    def test_non_finite_becomes_zero(self):
        for bad in (float("nan"), float("inf"), None, "0.1", True):
            vx, vy, vyaw = clamp_command(bad, bad, L)
            self.assertEqual((vx, vy, vyaw), (0.0, 0.0, 0.0), bad)
            self.assertTrue(all(math.isfinite(v) for v in (vx, vy, vyaw)))


class StateGateTest(unittest.TestCase):
    def test_standing_and_walking_allowed(self):
        self.assertEqual(state_problem(standing_state(), 0.1, L), "")
        self.assertEqual(state_problem(standing_state(policy="walk"), 0.1, L), "")

    def test_other_policies_blocked(self):
        for policy in ("sit", "rise", "roulade", "ground_pick", "kick_left", "held", "protection", None):
            self.assertNotEqual(state_problem(standing_state(policy=policy), 0.1, L), "", policy)

    def test_fallen_limp_unknown_blocked(self):
        self.assertIn("倒地", state_problem(standing_state(safety={"fallen": True, "limp": False}), 0.1, L))
        self.assertIn("倒地", state_problem(standing_state(safety={"fallen": False, "limp": True}), 0.1, L))
        self.assertIn("倒地", state_problem(standing_state(safety={}), 0.1, L))

    def test_stale_or_missing_state_blocked(self):
        self.assertNotEqual(state_problem(None, 0.1, L), "")
        self.assertIn("過期", state_problem(standing_state(), 0.6, L))
        self.assertIn("過期", state_problem(standing_state(), None, L))

    def test_bad_head_blocked(self):
        self.assertIn("頭部", state_problem(standing_state(head=[0, 0, float("nan"), 0]), 0.1, L))


class HealthGateTest(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(health_problem(healthy_health(), 0.1, L), "")

    def test_low_battery(self):
        problem = health_problem(healthy_health(battery={"volts": 6.96, "percent": 23}), 0.1, L)
        self.assertIn("請充電", problem)
        self.assertIn("請充電", health_problem(healthy_health(battery={"volts": 7.1, "percent": 40}), 0.1, L))
        # At and just above the 7.2 V floor: stopped; a little above it: allowed.
        self.assertIn("請充電", health_problem(healthy_health(battery={"volts": 7.15, "percent": 42}), 0.1, L))
        self.assertIn("請充電", health_problem(healthy_health(battery={"volts": 7.2, "percent": 43}), 0.1, L))
        self.assertEqual(health_problem(healthy_health(battery={"volts": 7.21, "percent": 43}), 0.1, L), "")
        self.assertIn("電池", health_problem(healthy_health(battery=None), 0.1, L))

    def test_imu(self):
        self.assertIn("IMU", health_problem(healthy_health(imu={"ready": False, "consecutive_stale_blocks": 0}), 0.1, L))
        self.assertIn("IMU", health_problem(healthy_health(imu={"ready": True, "consecutive_stale_blocks": 5}), 0.1, L))
        # Half the board's samples are stale, but short runs are tolerated, as in core.js.
        self.assertEqual(health_problem(healthy_health(imu={"ready": True, "stale_blocks": 6817, "consecutive_stale_blocks": 1}), 0.1, L), "")
        self.assertIn("IMU", health_problem(healthy_health(imu={"ready": True}), 0.1, L))

    def test_bus(self):
        self.assertIn("匯流排", health_problem(healthy_health(bus={"consecutive_errors": 1}), 0.1, L))
        self.assertEqual(health_problem(healthy_health(bus={"consecutive_errors": 1}), 0.1, Limits(max_bus_errors=1)), "")

    def test_thermal(self):
        self.assertIn("CPU", health_problem(healthy_health(cpu_temp_c=91.0), 0.1, L))
        self.assertIn("馬達", health_problem(healthy_health(motors={"hottest": "left_knee", "max_c": 70.0}), 0.1, L))

    def test_unhealthy_or_stale(self):
        self.assertIn("未通過", health_problem(healthy_health(healthy=False, reason="bus"), 0.1, L))
        self.assertIn("健康檢查", health_problem(healthy_health(), 0.7, L))
        self.assertIn("健康檢查", health_problem(None, 0.1, L))

    def test_combined_gate(self):
        self.assertEqual(motion_problem(standing_state(), 0.1, healthy_health(), 0.1, L), "")
        self.assertNotEqual(motion_problem(standing_state(policy="sit"), 0.1, healthy_health(), 0.1, L), "")
        self.assertNotEqual(motion_problem(standing_state(), 0.1, healthy_health(cpu_temp_c=95), 0.1, L), "")


if __name__ == "__main__":
    unittest.main()


class HotJointTest(unittest.TestCase):
    """--hot-joint: one servo whose reading runs high gets its own limit; the rest stay at 65 °C."""

    def health(self, hottest, c):
        return healthy_health(motors={"hottest": hottest, "max_c": c, "mean_c": 40.0})

    def test_off_by_default(self):
        from follow.safety import hard_problem
        self.assertIn("過熱", hard_problem(None, self.health("left_hip_roll", 70.0), Limits()))

    def test_only_the_named_joint_is_relaxed(self):
        from follow.safety import hard_problem
        L = Limits(hot_joint="left_hip_roll", hot_joint_max_c=75.0)
        self.assertEqual(hard_problem(None, self.health("left_hip_roll", 70.0), L), "")
        self.assertIn("過熱", hard_problem(None, self.health("left_hip_roll", 75.0), L))
        self.assertIn("過熱", hard_problem(None, self.health("right_knee", 66.0), L))
        self.assertIn("超過", health_problem(self.health("right_knee", 66.0), 0.1, L))
        self.assertEqual(health_problem(self.health("left_hip_roll", 70.0), 0.1, L), "")

    def test_bounds(self):
        for bad in (64.0, 86.0, float("nan")):
            with self.assertRaises(ValueError):
                Limits(hot_joint="left_hip_roll", hot_joint_max_c=bad)
