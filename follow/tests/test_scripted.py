"""--scripted: the whole show without a camera — walk straight on, dance, turn and walk straight back."""

import math
import unittest

from follow.app import FollowApp
from follow.tests.test_posture import PostureHarness
from follow.tests.test_straight import StraightWalkTest


class ScriptedShowTest(PostureHarness):
    push_state = StraightWalkTest.push_state
    tick = StraightWalkTest.tick

    def setUp(self):
        super().setUp()
        self.app.scripted = True
        self.app.straight_distance = 1.0
        self.app.straight_timeout = 30.0

    def say(self, command):
        self.commands.put(command, source="voice")

    def walk_until_idle(self, limit=400):
        for _ in range(limit):
            if self.app.mode == "idle":
                return
            self.tick(speed_scale=1.0)
        self.fail(f"still {self.app.mode}")

    def test_full_show_by_voice(self):
        self.say("letsgo")                      # off stage: walk on
        self.tick()
        self.assertEqual(self.app.mode, "straight")
        self.walk_until_idle()
        self.assertTrue(self.app.on_stage)
        on_stage_x = self.pos[0]
        self.assertGreaterEqual(on_stage_x, 0.95)

        self.say("dance")
        self.tick()
        self.assertEqual(self.app.mode, "dance")

        self.say("letsgo")                      # on stage (dancing): go home
        for _ in range(400):
            self.tick()
            self.yaw = self.yaw  # the harness integrates vyaw into yaw
            if self.app.mode == "straight":
                break
        self.assertEqual(self.app.mode, "straight", "turned, then walking back")
        self.assertTrue(any("轉身完成，直走回去" in line for line in self.logs))
        self.walk_until_idle()
        self.assertFalse(self.app.on_stage, "back off stage")
        for m in self.moves():
            self.assertEqual(m["vy"], 0.0)
            self.assertLessEqual(m["vx"], 0.12 + 1e-9)

    def test_keyboard_f_walks_straight(self):
        self.commands.put("follow")
        self.tick()
        self.assertEqual(self.app.mode, "straight")

    def test_letsgo_from_idle_on_stage_goes_home(self):
        self.app.on_stage = True
        self.say("letsgo")
        self.tick()
        self.assertEqual(self.app.mode, "home")

    def test_dance_returns_to_idle_not_gaze(self):
        self.say("dance")
        self.tick()
        self.assertEqual(self.app.mode, "dance")
        self.say("stop")
        self.tick()
        self.assertEqual(self.app.mode, "idle")


if __name__ == "__main__":
    unittest.main()
