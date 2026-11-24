import time
import unittest

from follow.motion import MotionGuard
from follow.safety import Limits
from follow.tests.fake_robotd import FakeRobotd, healthy_health, standing_state
from follow.tests.test_robotd_client import wait_for

FORBIDDEN = {"robot.enable", "robot.relax", "robot.do", "robot.pose", "robot.init"}


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRobotd()
        self.clock = FakeClock()
        self.fake.client._clock = self.clock
        self.logs = []
        self.guard = MotionGuard(self.fake.client, clock=self.clock, log=self.logs.append)
        self.guard.start(threads=False)
        self.push_state()

    def tearDown(self):
        self.guard.close()
        self.assertFalse(FORBIDDEN & set(self.fake.methods()), "guard must never change posture")
        self.fake.close()

    def push_state(self, **overrides):
        before = self.fake.client.state_at
        self.fake.push("robot.state", standing_state(**overrides))
        self.assertTrue(wait_for(lambda: self.fake.client.state_at is not None and
                                 (before is None or self.fake.client.state is not None and
                                  self.fake.client.state.get("policy") == overrides.get("policy", "stand"))))

    def sent_moves(self):
        time.sleep(0.02)
        return self.fake.moves()

    def test_start_subscribes_and_polls(self):
        self.assertEqual(self.fake.methods()[:2], ["robot.subscribe", "robot.health"])

    def test_move_is_clamped_and_vy_zero(self):
        self.assertEqual(self.guard.set_velocity(0.5, 2.0), "")
        self.assertEqual(self.guard.tick(), (0.12, 0.0, 0.5))
        self.assertEqual(self.sent_moves(), [{"vx": 0.12, "vy": 0.0, "vyaw": 0.5}])

    def test_intent_expires_and_stops(self):
        self.guard.set_velocity(0.1)
        self.guard.tick()
        self.clock.now += 0.5
        self.fake.client.state_at = self.clock.now
        self.guard.health_at = self.clock.now
        self.assertEqual(self.guard.tick(), (0.0, 0.0, 0.0))
        self.assertIn("robot.stop", self.fake.methods())
        self.assertFalse(self.guard.moving)

    def test_gate_failure_stops_and_does_not_resume(self):
        self.guard.set_velocity(0.1)
        self.guard.tick()
        self.push_state(policy="sit")
        self.assertEqual(self.guard.tick(), (0.0, 0.0, 0.0))
        self.assertIn("robot.stop", self.fake.methods())
        self.push_state(policy="stand")
        self.assertEqual(self.guard.tick(), (0.0, 0.0, 0.0), "must not resume without a new request")
        self.assertEqual(len(self.sent_moves()), 1)

    def test_refuses_when_gate_closed(self):
        self.push_state(policy="sit")
        self.assertNotEqual(self.guard.set_velocity(0.1), "")
        self.assertEqual(self.guard.tick(), (0.0, 0.0, 0.0))
        self.assertEqual(self.sent_moves(), [])

    def test_low_battery_latches(self):
        self.fake.health = healthy_health(battery={"volts": 6.96, "percent": 23})
        self.guard.poll_health()
        self.assertIn("請充電", self.guard.battery_low)
        self.assertTrue(any("請充電" in line for line in self.logs))
        self.fake.health = healthy_health()
        self.guard.poll_health()
        self.assertIn("請充電", self.guard.set_velocity(0.1))
        self.assertEqual(self.sent_moves(), [])

    def test_health_failure_blocks(self):
        self.fake.silent.add("robot.health")
        self.guard.poll_health()
        self.assertNotEqual(self.guard.set_velocity(0.1), "")

    def test_zero_request_stops(self):
        self.guard.set_velocity(0.1)
        self.guard.tick()
        self.guard.set_velocity(0.0)
        self.guard.tick()
        self.assertEqual(self.fake.methods().count("robot.stop"), 1)

    def test_close_sends_stop_even_after_exception(self):
        try:
            with self.guard:
                self.guard.set_velocity(0.1)
                self.guard.tick()
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertTrue(wait_for(lambda: "robot.stop" in self.fake.methods()))
        self.assertEqual(self.guard.set_velocity(0.1), "MotionGuard 已關閉")


class RaceAndFailureTest(GuardTest):
    def ordered_methods(self):
        self.fake.client.call("robot.mode")  # barrier: everything earlier has been recorded
        return self.fake.methods()

    def test_stop_between_copy_and_send_wins(self):
        """Review #1: the main thread halts and gets a stop acknowledged while the sender is
        between copying the intent and sending robot.move. No move may follow that stop."""
        self.guard.set_velocity(0.1)
        self.guard.set_head([0, 0, 0.1, 0])
        real_notify = self.fake.client.notify
        fired = []

        def interleave(method, params):
            real_notify(method, params)
            if method == "robot.head" and not fired:
                fired.append(True)
                self.assertTrue(self.guard.confirm_stopped())  # "main thread" runs here

        self.fake.client.notify = interleave
        try:
            sent = self.guard.tick()
        finally:
            self.fake.client.notify = real_notify
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        methods = self.ordered_methods()
        stop_at = methods.index("robot.stop")
        self.assertNotIn("robot.move", methods[stop_at:], methods)
        self.assertFalse(self.guard.moving)

    def test_move_before_stop_is_followed_by_the_stop(self):
        self.guard.set_velocity(0.1)
        self.guard.tick()
        self.assertTrue(self.guard.confirm_stopped())
        methods = self.ordered_methods()
        self.assertGreater(methods.index("robot.stop"), methods.index("robot.move"))
        self.assertEqual(self.guard.tick(), (0.0, 0.0, 0.0))

    def test_unexpected_exception_latches_and_stops(self):
        """Review #5: a worker exception must not leave a dead thread and a live intent."""
        self.guard.set_velocity(0.1)
        self.guard.tick()
        self.fake.health = ["not", "a", "dict"]
        self.guard.poll_health_safely()  # handled: treated as a failed health query
        self.assertIsNone(self.guard.health)
        original = self.guard.tick
        self.guard.tick = lambda: (_ for _ in ()).throw(KeyError("boom"))
        self.guard.tick_safely()
        self.guard.tick = original
        self.assertIn("內部錯誤", self.guard.failed)
        self.assertIn("robot.stop", self.ordered_methods())
        self.fake.health = healthy_health()
        self.guard.poll_health()
        self.push_state()
        self.assertIn("內部錯誤", self.guard.set_velocity(0.1), "latched until restart")
        self.assertIn("內部錯誤", self.guard.set_head([0, 0, 0, 0]))
        self.assertTrue(any("內部錯誤" in line for line in self.logs))

    def test_broken_log_does_not_break_halt(self):
        def broken(message):
            raise OSError("stdout closed")
        self.guard._log_raw = broken
        self.guard.set_velocity(0.1)
        self.guard.tick()
        self.guard.halt("reason")
        self.assertEqual(self.guard.tick(), (0.0, 0.0, 0.0))

    def test_stop_never_raises_on_odd_replies(self):
        self.fake.handlers["robot.stop"] = lambda p: ["odd"]
        self.assertTrue(self.fake.client.stop())
        self.fake.handlers["robot.stop"] = lambda p: {"accepted": False}
        self.assertFalse(self.fake.client.stop())


class ThreadedGuardTest(unittest.TestCase):
    def test_resends_faster_than_deadman(self):
        fake = FakeRobotd()
        guard = MotionGuard(fake.client, rate_hz=20, log=lambda s: None).start()
        try:
            fake.push("robot.state", standing_state())
            self.assertTrue(wait_for(lambda: fake.client.state is not None))
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                fake.push("robot.state", standing_state())
                guard.set_velocity(0.08)
                time.sleep(0.05)
            moves = fake.moves()
            self.assertGreaterEqual(len(moves), 6)
            self.assertTrue(all(m == {"vx": 0.08, "vy": 0.0, "vyaw": 0.0} for m in moves))
        finally:
            guard.close()
            fake.close()
        self.assertIn("robot.stop", fake.methods())
        self.assertFalse(FORBIDDEN & set(fake.methods()))


class LimitsWiringTest(unittest.TestCase):
    def test_custom_limits_still_capped(self):
        with self.assertRaises(ValueError):
            Limits(max_forward=0.5)


if __name__ == "__main__":
    unittest.main()
