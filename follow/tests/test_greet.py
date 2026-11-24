import unittest

from follow.greet import GREETING, Keyframe, Routine
from follow.tests.fake_robotd import standing_state
from follow.tests.test_app import AppHarness


class RoutineTest(unittest.TestCase):
    def test_greeting_timeline(self):
        r = Routine(GREETING, 10.0, [0, 0, 0, 0])
        heads, mouths, sounds = [], [], []
        now = 10.0
        while True:
            head, mouth, fired, done = r.update(now)
            heads.append(head)
            mouths.append(mouth)
            sounds += fired
            if done:
                break
            now += 0.05
        self.assertEqual(sounds, ["greet", "chirp"])
        self.assertAlmostEqual(max(h[2] for h in heads), 0.45)
        self.assertAlmostEqual(min(h[2] for h in heads), -0.45)
        self.assertEqual(heads[-1], (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(mouths[-1], 0.0)
        opens = sum(1 for a, b in zip(mouths, mouths[1:]) if a == 0 and b > 0)
        self.assertEqual(opens, 5, "three beak flaps with the greet, two with the chirp")
        for a, b in zip(heads, heads[1:]):  # smooth: no jumps between 50 ms samples
            self.assertLess(abs(b[2] - a[2]), 0.06)

    def test_starts_from_current_head(self):
        r = Routine([Keyframe(0.0), Keyframe(1.0, head=(0, 0, 0, 0))], 0.0, [0, 0, 0.4, 0])
        self.assertAlmostEqual(r.update(0.5)[0][2], 0.2)

    def test_sounds_fire_once(self):
        r = Routine(GREETING, 0.0)
        self.assertEqual(r.update(0.0)[2], ["greet"])
        self.assertEqual(r.update(0.01)[2], [])

    def test_validation(self):
        with self.assertRaises(ValueError):
            Routine([Keyframe(1.0), Keyframe(0.5)], 0)
        with self.assertRaises(ValueError):
            Routine([Keyframe(0.0, sound="wheee")], 0)
        with self.assertRaises(ValueError):
            Routine([Keyframe(0.0, mouth=2.0)], 0)
        with self.assertRaises(ValueError):
            Routine([], 0)


class GreetFlowTest(AppHarness):
    def run_greeting(self, before="idle"):
        if before != "idle":
            self.commands.put(before)
            self.step(marker_size=.4)
        self.commands.put("greet")
        for _ in range(70):
            self.step(marker_size=.4)
            if self.app.mode != "greet":
                break

    def test_greeting_plays_and_returns(self):
        self.run_greeting(before="gaze")
        self.assertEqual(self.app.mode, "gaze", "back to what it was doing")
        methods = self.methods()
        sounds = [m["params"]["tag"] for m in self.fake.received if m.get("method") == "robot.sound"]
        self.assertEqual(sounds, ["greet", "chirp"])
        mouths = [m["params"]["open"] for m in self.fake.received if m.get("method") == "robot.mouth"]
        self.assertGreater(max(mouths), 0.5)
        self.assertEqual(mouths[-1], 0.0)
        self.assertTrue(all(m["vx"] == 0 and m["vyaw"] == 0 for m in self.moves()))
        self.assertIn("robot.head", methods)

    def test_greeting_from_idle_returns_to_idle(self):
        self.run_greeting()
        self.assertEqual(self.app.mode, "idle")

    def test_fall_aborts_greeting(self):
        self.commands.put("greet")
        self.step()
        self.step()
        self.policy = "held"
        self.step()
        self.assertEqual(self.app.mode, "idle")
        count = len([m for m in self.fake.received if m.get("method") == "robot.mouth"])
        for _ in range(5):
            self.step()
        self.sync()
        later = len([m for m in self.fake.received if m.get("method") == "robot.mouth"])
        self.assertEqual(count, later, "no mouth commands once the state gate fails")

    def test_close_shuts_the_mouth(self):
        self.commands.put("greet")
        for _ in range(3):
            self.step()
        self.guard.close()
        self.sync()
        mouths = [m["params"]["open"] for m in self.fake.received if m.get("method") == "robot.mouth"]
        self.assertEqual(mouths[-1], 0.0)


class GreetDryRunTest(AppHarness):
    dry_run = True

    def test_dry_run_is_silent(self):
        self.commands.put("greet")
        for _ in range(60):
            self.step()
        self.assertFalse({"robot.sound", "robot.mouth", "robot.head", "robot.move"} & set(self.methods()))
        self.assertEqual(self.guard.last_command.get("sound"), "chirp")


if __name__ == "__main__":
    unittest.main()
