"""The whole 12/03 show, end to end, against the fake robotd (no hardware).

Boss flow: off stage the boss says "Let's go!" → the duck follows him on stage → he stops →
"Let's party!" at the money gun → the duck dances on the spot → "Let's go home" → it turns
round and follows him off. Plus the things that go wrong on a real stage: an extra "Let's go"
mid-walk, a misheard word mid-dance, nobody saying anything after the dance.

World model (1-D, along the walk): host at `host` m, duck at `duck` m; the duck advances by the
vx actually sent. The marker's apparent size is 0.4 at the follow distance (the tracker's
target) and scales with 1/distance.
"""

import re
import unittest

from follow.greet import DANCE_MAX
from follow.tests.test_posture import PostureHarness

FOLLOW_DISTANCE = 1.0  # m at which the marker looks 0.4 (the tracker's target size)


class ShowRehearsalTest(PostureHarness):
    def test_average_follow_speed(self):
        """How fast the duck really covers ground behind a far host (for the runbook)."""
        self.commands.put("follow")
        for _ in range(300):
            self.host = self.duck + 2.5  # always 2.5 m ahead: it keeps pulsing
            self.tick()
        average = self.duck / 30.0
        print(f"\n[rehearsal] 平均跟隨速度 {average:.3f} m/s（上限 0.12）")
        self.assertGreater(average, 0.04)
        self.assertLess(average, 0.12)

    def test_forward_option_reaches_the_tracker(self):
        """--forward used to raise only MotionGuard's cap; the pulses still asked for 0.12."""
        from follow.app import default_tracker_options
        from follow.safety import Limits
        from follow.tracker import Tracker
        self.assertEqual(default_tracker_options().forward, 0.12)
        self.assertEqual(default_tracker_options(0, 0.30).forward, 0.25)
        self.guard.limits = Limits(max_forward=0.2)
        self.app.tracker = Tracker(default_tracker_options(0, 0.2))
        self.commands.put("follow")
        for _ in range(100):
            self.host = self.duck + 2.5
            self.tick()
        fastest = max(m["vx"] for m in self.moves())
        self.assertGreater(fastest, 0.15)
        self.assertLessEqual(fastest, 0.2 + 1e-9)
        print(f"\n[rehearsal] --forward 0.2 平均跟隨速度 {self.duck / 10.0:.3f} m/s")

    def setUp(self):
        super().setUp()
        self.host, self.duck = 2.5, 0.0
        self.host_speed = 0.0

    def tick(self, n=1, dt=0.1, marker=True):
        for _ in range(n):
            self.host += self.host_speed * dt
            distance = max(0.3, self.host - self.duck)
            size = min(0.9, 0.4 * FOLLOW_DISTANCE / distance) if marker else None
            out, sent = self.step(dt, marker_size=size)
            self.duck += sent[0] * dt

    def say(self, command):
        self.commands.put(command, source="voice")

    def modes(self):
        return [m.group(1, 2) for m in (re.search(r"模式 (\w+) → (\w+)", l) for l in self.logs) if m]

    def assert_never_walked_fast_or_sideways(self):
        for move in self.moves():
            self.assertEqual(move["vy"], 0.0)
            self.assertLessEqual(move["vx"], 0.12 + 1e-9)
            self.assertGreaterEqual(move["vx"], -0.12 - 1e-9)

    def test_full_show(self):
        # 0. During the presentation: standing at the side, the boss far away. Nothing moves.
        self.tick(20)
        self.assertEqual(self.moves(), [])

        # 1. "Let's go!" — the boss walks on stage at a slow 0.1 m/s for 6 s.
        self.say("letsgo")
        self.host_speed = 0.1
        self.tick(30)
        self.assertEqual(self.app.mode, "follow")
        self.say("letsgo")  # he says it again mid-walk: must not turn the duck round
        self.tick(30)
        self.assertEqual(self.app.mode, "follow")
        self.assertIsNone(self.app.turn)
        # 2. He stops at his mark; the duck closes in and stands.
        self.host_speed = 0.0
        self.tick(250)  # the duck averages well under its 0.12 m/s cap (pulse, stop, settle, look)
        self.assertGreater(self.duck, 0.4, "it came along")
        self.assertLess(self.host - self.duck, 1.6, "and ended up near him")
        moves_before_dance = len(self.moves())

        # 3. "Let's party!" at the money gun: dance on the spot.
        self.say("dance")
        self.tick(50)
        self.assertEqual(self.app.mode, "dance")
        dance_moves = self.moves()[moves_before_dance:]
        self.assertEqual([m for m in dance_moves if m["vx"] or m["vyaw"]], [], "no walking while dancing")
        self.assertTrue([p for p in self.sent("robot.pose") if p.get("active")], "the body sways")
        self.assertTrue(self.sent("robot.sound"))

        # 4. "Let's go home" (heard as just "LET'S GO" — the clipped case): turn and follow off.
        position_at_dance_end = self.duck
        self.say("letsgo")
        self.tick()
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False, "body upright before walking")
        self.assertEqual(self.sent("robot.mouth")[-1]["open"], 0.0)
        for _ in range(200):
            if self.app.mode == "follow":
                break
            self.tick(marker=False)  # turning: the host is behind the duck for now
        self.assertEqual(self.app.mode, "follow")
        self.assertGreater(abs(self.yaw), 2.9, "turned about 180°")
        # Walking off: in the 1-D model "away" is still +x for the host relative to the duck.
        self.host = self.duck + 1.5
        self.host_speed = 0.1
        self.tick(60)
        self.assertGreater(self.duck, position_at_dance_end + 0.1, "followed him off")

        # Off stage: the operator ends it.
        self.commands.put("stop")
        self.tick()
        self.commands.put("quit")
        self.tick()
        self.assertFalse(self.app.running)
        self.assert_never_walked_fast_or_sideways()
        self.assertEqual(
            self.modes(),
            [("idle", "follow"), ("follow", "idle"), ("idle", "dance"), ("dance", "home"),
             ("home", "follow"), ("follow", "idle")])

    def test_dance_ends_itself_then_home_still_works(self):
        self.say("letsgo")
        self.tick(20)
        self.say("dance")
        self.tick(5)
        self.assertEqual(self.app.mode, "dance")
        self.tick(int(DANCE_MAX / 0.1) + 5)  # nobody says anything for 30 s
        self.assertEqual(self.app.mode, "gaze", "back to watching the host")
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)
        self.say("letsgo")
        self.tick()
        self.assertEqual(self.app.mode, "home")

    def test_misheard_words_mid_dance(self):
        self.say("letsgo")
        self.tick(20)
        self.say("dance")
        self.tick(10)
        # Crowd noise heard as "SIT DOWN" / "GET UP": posture words from voice while dancing
        # are handled by the posture gates; the duck must never be left posed or walking.
        self.say("sit")
        self.tick(5)
        self.assertNotEqual(self.app.mode, "follow")
        # "STOP" is always obeyed: dance ends, body upright, standing.
        self.say("stop")
        self.tick()
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)
        # Recover by voice from idle: "Let's go" means follow again (the operator can press h instead).
        self.say("letsgo")
        self.tick()
        self.assertEqual(self.app.mode, "follow")

    def test_keyboard_only_show(self):
        """The same show when the microphone fails completely: f → d → h."""
        self.commands.put("follow")
        self.host_speed = 0.1
        self.tick(40)
        self.host_speed = 0.0
        self.tick(60)
        self.commands.put("dance")
        self.tick(20)
        self.assertEqual(self.app.mode, "dance")
        self.commands.put("home")
        for _ in range(200):
            self.tick(marker=False)
            if self.app.mode == "follow":
                break
        self.assertEqual(self.app.mode, "follow")
        self.assert_never_walked_fast_or_sideways()


class CliTest(unittest.TestCase):
    def test_kws_threshold_range(self):
        from follow import app
        for bad in ("0.05", "0.9"):
            with self.assertRaises(SystemExit):
                app.main(["--kws-threshold", bad, "--replay", "nonexistent.jsonl"])


if __name__ == "__main__":
    unittest.main()
