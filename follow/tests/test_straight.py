"""`k`: walk straight ahead a fixed distance by odometry, with a timeout backstop."""

import unittest

from follow.app import STRAIGHT_DISTANCE, STRAIGHT_TIMEOUT
from follow.tests.test_posture import PostureHarness


class StraightWalkTest(PostureHarness):
    def tick(self, n=1, dt=0.1, speed_scale=0.6):
        """Advance; the duck covers `speed_scale` × the commanded vx (it really under-delivers)."""
        if not hasattr(self, "pos"):
            self.pos = [0.0, 0.0]
        for _ in range(n):
            out, sent = self.step(dt)
            self.pos[0] += sent[0] * dt * speed_scale

    def push_state(self):
        from follow.tests.fake_robotd import standing_state
        from follow.tests.test_app import wait_for
        pos = getattr(self, "pos", [0.0, 0.0])
        self.fake.push("robot.state", standing_state(
            policy=self.policy, odom={"position": [pos[0], pos[1], 0.12], "yaw": 0.0}))
        self.assertTrue(wait_for(lambda: self.fake.client.state_at == self.clock.now
                                 and self.fake.client.state.get("policy") == self.policy))

    def assert_ends_stopped(self):
        self.tick(2)
        methods = self.methods()
        last_move = max((i for i, m in enumerate(methods) if m == "robot.move"), default=-1)
        last_stop = max((i for i, m in enumerate(methods) if m == "robot.stop"), default=-1)
        self.assertGreater(last_stop, last_move, "a robot.stop after the last robot.move")

    def test_walks_until_two_metres_then_stops(self):
        self.commands.put("straight")
        self.tick()
        self.assertEqual(self.app.mode, "straight")
        for _ in range(int(STRAIGHT_TIMEOUT / 0.1) + 20):
            if self.app.mode != "straight":
                break
            self.tick(speed_scale=1.0)
        self.assertEqual(self.app.mode, "idle")
        self.assertGreaterEqual(self.pos[0], STRAIGHT_DISTANCE - 0.05)
        self.assertLess(self.pos[0], STRAIGHT_DISTANCE + 0.2)
        for m in self.moves():
            self.assertEqual(m["vy"], 0.0)
            self.assertEqual(m["vyaw"], 0.0, "no heading correction until odom.yaw's sign is verified")
            self.assertLessEqual(m["vx"], 0.12 + 1e-9)
        self.assert_ends_stopped()

    def test_timeout_is_the_backstop(self):
        self.commands.put("straight")
        self.tick()
        for _ in range(int(STRAIGHT_TIMEOUT / 0.1) + 20):
            if self.app.mode != "straight":
                break
            self.tick(speed_scale=0.1)  # odom barely moves
        self.assertEqual(self.app.mode, "idle")
        self.assertTrue(any("時間到" in line for line in self.logs))

    def test_stop_key_ends_it(self):
        self.commands.put("straight")
        self.tick(10)
        self.commands.put("stop")
        self.tick()
        self.assertEqual(self.app.mode, "idle")
        self.assert_ends_stopped()

    def test_refused_when_not_standing(self):
        self.set_state("sit")
        self.commands.put("straight")
        self.tick(3)
        self.assertNotEqual(self.app.mode, "straight")
        self.assertEqual([m for m in self.moves() if m["vx"]], [])


if __name__ == "__main__":
    unittest.main()
