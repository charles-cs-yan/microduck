import math
import unittest

from follow.turning import DONE, FAILED, TURNING, Turn, TurnOptions, odom_yaw, wrap


def simulate(turn, yaw=0.0, dt=0.1, efficiency=1.0, steps=400, start=0.0):
    """Integrate the commanded vyaw into a heading; returns (final status, yaw, commands)."""
    now, commands = start, []
    status = turn.status
    for _ in range(steps):
        now += dt
        vyaw, status = turn.update(now, wrap(yaw))
        if status != TURNING:
            break
        commands.append(vyaw)
        yaw += vyaw * dt * efficiency
    return status, yaw, commands


class WrapTest(unittest.TestCase):
    def test_wrap(self):
        self.assertAlmostEqual(wrap(math.pi + 0.1), -math.pi + 0.1)
        self.assertAlmostEqual(wrap(-math.pi - 0.1), math.pi - 0.1)
        self.assertAlmostEqual(wrap(0.3), 0.3)

    def test_odom_yaw(self):
        self.assertEqual(odom_yaw({"odom": {"yaw": 0.5}}), 0.5)
        for bad in (None, {}, {"odom": {}}, {"odom": {"yaw": float("nan")}}, {"odom": {"yaw": True}}):
            self.assertIsNone(odom_yaw(bad))


class TurnTest(unittest.TestCase):
    def test_half_turn_left_across_the_wrap(self):
        start = 2.9  # crossing +π during the turn must not confuse it
        turn = Turn(math.pi, start, 0.0)
        status, yaw, commands = simulate(turn, yaw=start)
        self.assertEqual(status, DONE)
        self.assertLess(abs(wrap(yaw - (start + math.pi))), 0.08 + 1e-9)
        self.assertTrue(all(0 <= v <= 0.40 for v in commands))
        self.assertGreater(max(commands), 0.39)

    def test_right_turn(self):
        turn = Turn(-0.5, -3.0, 0.0)
        status, yaw, commands = simulate(turn, yaw=-3.0)
        self.assertEqual(status, DONE)
        self.assertTrue(all(v <= 0 for v in commands))
        self.assertLess(abs(wrap(yaw - (-3.5))), 0.081)

    def test_ramp_and_min_rate(self):
        turn = Turn(1.0, 0.0, 0.0)
        status, yaw, commands = simulate(turn)
        self.assertLessEqual(commands[0], 0.5 * 0.1 + 1e-9, "acceleration limited from standstill")
        later = commands[10:]
        self.assertTrue(all(abs(v) >= 0.15 - 1e-9 for v in later), "never crawls below min_rate")

    def test_overshoot_is_corrected(self):
        turn = Turn(0.6, 0.0, 0.0)
        now, yaw = 0.0, 0.0
        for _ in range(30):
            now += 0.1
            vyaw, status = turn.update(now, yaw)
            if status != TURNING:
                break
            yaw += vyaw * 0.1 * 1.6  # robot turns 60 % more than asked
        self.assertIn(turn.status, (DONE, TURNING))
        status, yaw, _ = simulate(turn, yaw=yaw, start=now, efficiency=1.0)
        self.assertEqual(status, DONE)
        self.assertLess(abs(yaw - 0.6), 0.081)

    def test_stuck_heading_fails(self):
        turn = Turn(math.pi, 0.0, 0.0)
        status, yaw, commands = simulate(turn, efficiency=0.0)
        self.assertEqual(status, FAILED)
        self.assertIn("沒有變化", turn.reason)
        self.assertLessEqual(len(commands), 21)

    def test_slow_robot_times_out(self):
        turn = Turn(math.pi, 0.0, 0.0)
        status, yaw, commands = simulate(turn, efficiency=0.2)  # always progressing, never arriving
        self.assertEqual(status, FAILED)
        self.assertIn("逾時", turn.reason)
        self.assertLessEqual(len(commands) * 0.1, turn.deadline + 0.11)

    def test_wrong_sign_fails_fast(self):
        """Review #3: if odom.yaw turns the other way, stop — do not spin until the deadline."""
        turn = Turn(math.pi, 0.0, 0.0)
        status, yaw, commands = simulate(turn, efficiency=-1.0)
        self.assertEqual(status, FAILED)
        self.assertIn("反方向", turn.reason)
        self.assertLess(abs(yaw), 0.15 + 0.05)
        self.assertLessEqual(len(commands) * 0.1, 1.5)

    def test_odom_jump_past_target_fails(self):
        turn = Turn(0.6, 0.0, 0.0)
        turn.update(0.1, 0.0)
        vyaw, status = turn.update(0.2, 1.0)  # odom re-anchors: +1 rad in one tick
        self.assertEqual(status, FAILED)
        self.assertIn("轉過頭", turn.reason)

    def test_deadline_is_bounded(self):
        turn = Turn(math.pi, 0.0, 0.0)
        self.assertLess(turn.deadline, 16.0)
        self.assertGreater(turn.deadline, math.pi / 0.4 + 1)

    def test_missing_heading(self):
        self.assertEqual(Turn(1.0, None, 0.0).status, FAILED)
        turn = Turn(1.0, 0.0, 0.0)
        self.assertEqual(turn.update(0.1, None), (0.0, FAILED))
        self.assertEqual(turn.update(0.2, 0.0), (0.0, FAILED), "stays failed")

    def test_option_limits(self):
        with self.assertRaises(ValueError):
            TurnOptions(max_rate=0.6)
        with self.assertRaises(ValueError):
            TurnOptions(min_rate=0.5, max_rate=0.4)
        with self.assertRaises(ValueError):
            Turn(4.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            Turn(float("nan"), 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
