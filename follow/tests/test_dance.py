"""Dance (money-gun celebration) and `--home-turn 0`: routine logic and the app flow."""

import math
import unittest

from follow.app import FollowApp
from follow.greet import DANCE, DANCE_LAP, DANCE_MAX, Keyframe, Routine
from follow.posture import TILT_LIMIT, Z_RANGE
from follow.tests.fake_robotd import standing_state
from follow.tests.test_posture import PostureHarness
from follow.tests.test_robotd_client import wait_for


class DanceRoutineTest(unittest.TestCase):
    def sample(self, routine, start, seconds, dt=0.05):
        out, now = [], start
        while now <= start + seconds + 1e-9:
            head, mouth, sounds, done = routine.update(now)
            out.append((now, head, mouth, sounds, routine.pose, done))
            now += dt
        return out

    def test_lap_is_closed_and_in_range(self):
        self.assertEqual(DANCE[0].pose, (0.0, 0.0, 0.0))
        self.assertEqual(DANCE[-1].pose, DANCE[0].pose)
        self.assertEqual(DANCE[-1].head, DANCE[0].head)
        self.assertEqual(DANCE[-1].t, DANCE_LAP)
        for frame in DANCE:
            if frame.pose:
                z, roll, pitch = frame.pose
                self.assertTrue(Z_RANGE[0] <= z <= Z_RANGE[1] and abs(roll) <= TILT_LIMIT and abs(pitch) <= TILT_LIMIT)

    def test_loops_until_max_then_done(self):
        r = Routine(DANCE, 100.0, name="跳舞", loop_until=DANCE_MAX)
        samples = self.sample(r, 100.0, DANCE_MAX + 0.5)
        dones = [s[5] for s in samples]
        first_done = dones.index(True)
        self.assertAlmostEqual(samples[first_done][0] - 100.0, DANCE_MAX, delta=0.06)
        self.assertTrue(all(dones[first_done:]))
        sounds = [tag for s in samples for tag in s[3]]
        laps = math.ceil(DANCE_MAX / DANCE_LAP)
        self.assertEqual(sounds.count("laugh"), laps, "one laugh per lap, never a burst")
        self.assertEqual(samples[-1][2], 0.0, "beak closed at the end")

    def test_smooth_sway_both_ways(self):
        r = Routine(DANCE, 0.0, name="跳舞", loop_until=DANCE_MAX)
        samples = self.sample(r, 0.0, 3 * DANCE_LAP)
        rolls = [s[4][1] for s in samples]
        self.assertGreater(max(rolls), 0.1)
        self.assertLess(min(rolls), -0.1)
        for a, b in zip(samples, samples[1:]):  # no jumps between 50 ms samples, also across laps
            self.assertLess(abs(b[4][1] - a[4][1]), 0.02)
            self.assertLess(abs(b[4][0] - a[4][0]), 0.003)
            self.assertLess(abs(b[1][2] - a[1][2]), 0.04)

    def test_greeting_has_no_pose(self):
        from follow.greet import GREETING
        r = Routine(GREETING, 0.0)
        r.update(1.0)
        self.assertIsNone(r.pose)

    def test_pose_validation(self):
        for bad in ((0.05, 0, 0), (0, 0.4, 0), (0, 0, -0.3), (0, 0), (float("nan"), 0, 0)):
            with self.assertRaises(ValueError, msg=bad):
                Routine([Keyframe(0.0, pose=bad)], 0)
        with self.assertRaises(ValueError):
            Routine([Keyframe(0.0)], 0, loop_until=5.0)  # zero-length lap
        with self.assertRaises(ValueError):
            Routine(DANCE, 0, loop_until=-1)


class DanceFlowTest(PostureHarness):
    def poses(self, active=True):
        return [p for p in self.sent("robot.pose") if p.get("active") is active]

    def test_dance_on_the_spot_then_stop_restores(self):
        self.commands.put("dance")
        for _ in range(45):
            self.step()
        self.assertEqual(self.app.mode, "dance")
        rolls = [p["roll"] for p in self.poses()]
        self.assertGreater(len(rolls), 30)
        self.assertGreater(max(rolls), 0.1)
        self.assertLess(min(rolls), -0.1)
        self.assertTrue(self.sent("robot.sound"), "it makes noise")
        self.assertTrue(any(m["open"] > 0 for m in self.sent("robot.mouth")), "beak chatters")
        self.commands.put("stop")
        self.step()
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False, "body back to nominal")
        self.assertEqual(self.sent("robot.mouth")[-1]["open"], 0.0, "beak closed")
        self.assertEqual([m for m in self.moves() if m["vx"] or m["vyaw"] or m.get("vy")], [], "never walks")

    def test_ends_itself_and_returns_to_gaze(self):
        self.commands.put("gaze")
        self.step(marker_size=.4)
        self.commands.put("dance")
        self.step(marker_size=.4)
        self.assertEqual(self.app.mode, "dance")
        steps = int(DANCE_MAX / 0.1) + 5
        for _ in range(steps):
            self.step(marker_size=.4)
            if self.app.mode != "dance":
                break
        self.assertEqual(self.app.mode, "gaze")
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)
        self.assertTrue(any("跳舞結束" in l for l in self.logs))

    def test_voice_home_accepted_while_dancing(self):
        self.commands.put("dance")
        for _ in range(5):
            self.step()
        self.commands.put("home", source="voice")
        self.step()
        self.assertIn(self.app.mode, ("home", "follow"))
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False, "pose released before walking")

    def walking_tick(self, dt=0.1):
        """One tick while robotd still reports the gait moving (not `still`)."""
        self.clock.now += dt
        self.fake.push("robot.state", standing_state(policy="walk", move={"requested": [0, 0, 0], "applied": [0.1, 0, 0]}))
        self.assertTrue(wait_for(lambda: self.fake.client.state_at == self.clock.now))
        self.guard.poll_health()
        self.app.tick()
        return self.guard.tick()

    def test_voice_dance_mid_follow_when_still(self):
        self.commands.put("follow")
        self.step(marker_size=.4)
        self.commands.put("dance", source="voice")
        self.step(marker_size=.4)
        self.assertEqual(self.app.mode, "dance")
        self.assertEqual(self.app.return_mode, "gaze", "after the dance it keeps watching the host")

    def test_voice_dance_mid_pulse_waits_until_still(self):
        self.commands.put("follow")
        self.step(marker_size=.4)
        self.commands.put("dance", source="voice")
        self.walking_tick()
        self.assertEqual(self.app.mode, "idle", "stopped first")
        self.walking_tick()
        self.assertEqual(self.app.mode, "idle", "still walking: not yet")
        self.assertEqual(self.poses(), [])
        self.step()
        self.assertEqual(self.app.mode, "dance")

    def test_deferred_dance_expires_and_is_cancelled(self):
        self.commands.put("follow")
        self.step(marker_size=.4)
        self.commands.put("dance", source="voice")
        for _ in range(35):  # 3.5 s of the gait never settling
            self.walking_tick()
        self.step()
        self.assertEqual(self.app.mode, "idle", "too late: nothing starts by itself")
        self.commands.put("follow")
        self.step(marker_size=.4)
        self.commands.put("dance", source="voice")
        self.walking_tick()
        self.commands.put("stop")
        self.walking_tick()
        self.step()
        self.assertEqual(self.app.mode, "idle", "stop cancels the waiting dance")
        self.assertEqual(self.poses(), [])

    def test_voice_dance_refused_while_going_home(self):
        self.app.home_angle = 0.0
        self.commands.put("gaze")
        self.step(marker_size=.4)
        self.commands.put("home", source="voice")
        self.step(marker_size=.4)
        self.app.mode = "home"  # as during the home turn
        self.commands.put("dance", source="voice")
        self.step(marker_size=.4)
        self.assertNotEqual(self.app.mode, "dance")
        self.assertEqual(self.poses(), [])

    def test_refused_unless_standing(self):
        self.set_state("sit")
        self.commands.put("dance")
        self.step_as("sit")
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.poses(), [])

    def test_fall_mid_dance_goes_idle_and_stops_posing(self):
        self.commands.put("dance")
        for _ in range(5):
            self.step()
        self.step_as("held", fallen=True, limp=True)
        self.assertEqual(self.app.mode, "idle")
        count = len(self.poses())
        for _ in range(5):
            self.step_as("held", fallen=True, limp=True)
        self.assertEqual(len(self.poses()), count)
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)

    def test_close_mid_dance_restores_body(self):
        self.commands.put("dance")
        for _ in range(3):
            self.step()
        self.guard.close()
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)


class LetsGoTest(PostureHarness):
    """Voice "LET'S GO" means come with me (from idle) or go home (standing on stage)."""

    def test_from_idle_follows(self):
        self.commands.put("letsgo", source="voice")
        self.step(marker_size=.4)
        self.assertEqual(self.app.mode, "follow")

    def test_from_gaze_or_dance_goes_home(self):
        for before in ("gaze", "dance"):
            self.commands.put("stop")
            self.step(marker_size=.4)
            self.commands.put(before)
            self.step(marker_size=.4)
            self.assertEqual(self.app.mode, before)
            self.commands.put("letsgo", source="voice")
            self.step(marker_size=.4)
            self.assertIn(self.app.mode, ("home", "idle"), before)  # idle: the dry turn may fail
            self.assertTrue(any("回家轉身" in l for l in self.logs), before)
            self.logs.clear()

    def test_while_following_does_nothing(self):
        self.commands.put("follow")
        self.step(marker_size=.4)
        self.commands.put("letsgo", source="voice")
        self.step(marker_size=.4)
        self.assertEqual(self.app.mode, "follow")
        self.assertIsNone(self.app.turn)

    def test_no_keyboard_alias(self):
        from follow.sources import parse_command
        self.assertIsNone(parse_command("letsgo"))
        self.assertEqual(parse_command("let's go home"), "home")


class HomeTurnOptionTest(PostureHarness):
    def test_zero_follows_without_turning(self):
        self.app.home_angle = 0.0
        self.commands.put("home")
        self.step(marker_size=.4)
        self.assertEqual(self.app.mode, "follow")
        self.assertIsNone(self.app.turn)
        self.assertTrue(any("不做固定轉身" in l for l in self.logs))

    def test_validation(self):
        for bad in (0.3, -0.2, 4.0, float("nan")):
            with self.assertRaises(ValueError, msg=bad):
                FollowApp(self.guard, self.source, self.commands, home_angle=bad)
        FollowApp(self.guard, self.source, self.commands, home_angle=0.0)
        FollowApp(self.guard, self.source, self.commands, home_angle=-math.pi / 2)

    def test_cli_rejects_bad_degrees(self):
        from follow import app
        with self.assertRaises(SystemExit):
            app.main(["--home-turn", "10", "--replay", "nonexistent.jsonl"])


if __name__ == "__main__":
    unittest.main()
