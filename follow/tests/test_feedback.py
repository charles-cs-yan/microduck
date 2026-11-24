"""Audible feedback: the duck answers a spoken command, and "asks" when it cannot see the marker."""

import unittest

from follow.app import ACK_SOUNDS, LOST_HINT_AFTER, LOST_HINT_REPEAT
from follow.tests.test_posture import PostureHarness


class VoiceAckTest(PostureHarness):
    def sounds(self):
        return [p["tag"] for p in self.sent("robot.sound")]

    def test_voice_follow_chirps(self):
        self.commands.put("follow", source="voice")
        self.step(marker_size=0.4)
        self.assertEqual(self.app.mode, "follow")
        self.assertEqual(self.sounds(), [ACK_SOUNDS["follow"]])

    def test_keyboard_is_silent(self):
        self.commands.put("follow")
        self.step(marker_size=0.4)
        self.assertEqual(self.sounds(), [])

    def test_each_mode_has_its_own_call(self):
        self.commands.put("gaze", source="voice")
        self.step(marker_size=0.4)
        self.commands.put("stop", source="voice")
        self.step(marker_size=0.4)
        self.assertEqual(self.sounds(), [ACK_SOUNDS["gaze"], ACK_SOUNDS["idle"]])

    def test_heard_but_not_acted_on_asks(self):
        self.commands.put("home", source="voice")  # refused while idle
        self.step(marker_size=0.4)
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.sounds(), ["inquire"])

    def test_dance_makes_its_own_sound_only(self):
        self.commands.put("dance", source="voice")
        self.step(marker_size=0.4)
        self.assertEqual(self.app.mode, "dance")
        self.assertNotIn("inquire", self.sounds())
        self.assertNotIn(ACK_SOUNDS["idle"], self.sounds())


class LostHintTest(PostureHarness):
    def hints(self):
        return [p for p in self.sent("robot.sound") if p["tag"] == "inquire"]

    def test_asks_when_no_marker_then_repeats(self):
        self.commands.put("gaze")
        self.step(marker_size=None)
        for _ in range(int(LOST_HINT_AFTER / 0.1) - 3):
            self.step(marker_size=None)
        self.assertEqual(self.hints(), [], "not before the delay")
        for _ in range(6):
            self.step(marker_size=None)
        self.assertEqual(len(self.hints()), 1)
        for _ in range(int(LOST_HINT_REPEAT / 0.1) - 10):
            self.step(marker_size=None)
        self.assertEqual(len(self.hints()), 1, "not again too soon")
        for _ in range(15):
            self.step(marker_size=None)
        self.assertEqual(len(self.hints()), 2)

    def test_silent_while_the_marker_is_seen(self):
        self.commands.put("gaze")
        for _ in range(int(LOST_HINT_AFTER / 0.1) * 3):
            self.step(marker_size=0.4)
        self.assertEqual(self.hints(), [])

    def test_idle_never_asks(self):
        for _ in range(int(LOST_HINT_AFTER / 0.1) * 2):
            self.step(marker_size=None)
        self.assertEqual(self.hints(), [])


if __name__ == "__main__":
    unittest.main()
