import io
import math
import unittest

from follow import app as app_module
from follow.app import FollowApp
from follow.motion import MotionGuard
from follow.sources import CommandQueue, Frame, ReplaySource, parse_command
from follow.tests.fake_robotd import FakeRobotd, standing_state
from follow.tests.test_robotd_client import wait_for
from follow.tests.test_tracker import picture
from follow.turning import wrap

FORBIDDEN = {"robot.enable", "robot.relax", "robot.do", "robot.pose", "robot.init"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScriptedSource:
    """Camera stand-in: each step the test decides what the newest frame is."""
    live = False

    def __init__(self):
        self.frame = None

    def show(self, detections, captured_at):
        self.frame = Frame(captured_at=captured_at, video_at=captured_at, detections=detections)

    def latest(self):
        return self.frame


class AppHarness(unittest.TestCase):
    dry_run = False
    # Follow/gaze/home/greet flows never change posture. Posture tests override this with
    # NEVER_SENT: relax and init are not sent by anything in follow/, ever.
    forbidden = FORBIDDEN

    def setUp(self):
        self.fake = FakeRobotd()
        self.clock = Clock()
        self.fake.client._clock = self.clock
        self.logs = []
        self.guard = MotionGuard(self.fake.client, clock=self.clock, log=self.logs.append,
                                 dry_run=self.dry_run, mode_period=0.0)
        self.guard.start(threads=False)
        self.source = ScriptedSource()
        self.commands = CommandQueue()
        self.app = FollowApp(self.guard, self.source, self.commands, clock=self.clock, log=self.logs.append)
        self.policy = "stand"
        # Tiny world model: body heading integrates the vyaw actually sent (sim_turn scales it;
        # 0 models a duck that does not turn), and odom can be withheld entirely.
        self.yaw = 0.0
        self.sim_turn = 1.0
        self.odom = True
        self.last_sent = (0.0, 0.0, 0.0)
        self.push_state()

    def tearDown(self):
        self.guard.close()
        self.assertFalse(self.forbidden & set(self.fake.methods()), "a forbidden robotd method was sent")
        self.fake.close()

    def push_state(self):
        extra = {"odom": {"position": [0, 0, 0.2], "yaw": wrap(self.yaw)}} if self.odom else {}
        self.fake.push("robot.state", standing_state(policy=self.policy, **extra))
        self.assertTrue(wait_for(lambda: self.fake.client.state_at == self.clock.now
                                 and self.fake.client.state.get("policy") == self.policy))

    def step(self, dt=0.1, marker_size=None, x=0.0):
        """Advance time, refresh robot state/health, optionally show a marker, run one tick."""
        self.clock.now += dt
        self.yaw += self.last_sent[2] * dt * self.sim_turn
        self.push_state()
        self.guard.poll_health()
        if marker_size is not None:
            p = picture(x, 0, marker_size)
            p["boxes"][0]["id"] = 0
            self.source.show(p, self.clock.now)
        elif self.source.frame is not None:
            # keep video alive but with no marker in view
            self.source.show({"width": 720, "height": 1280, "boxes": []}, self.clock.now)
        out = self.app.tick()
        sent = self.guard.tick()
        self.last_sent = sent
        return out, sent

    def step_world(self, bearing, size=.4, half_fov=0.6, dt=0.1):
        """Show a marker at a fixed world bearing (rad, +left); the image position depends on
        where the body and head point. Out of the field of view → no marker in the frame."""
        head = self.guard.last_command.get("head") or (0.0, 0.0, 0.0, 0.0)
        x = -(wrap(bearing - self.yaw - head[2])) / half_fov
        if abs(x) <= 0.8:
            return self.step(dt, marker_size=size, x=x)
        self.source.show({"width": 720, "height": 1280, "boxes": []}, self.clock.now + dt)
        return self.step(dt)

    def sync(self):
        """Barrier: the fake answers in order, so once this reply is back every earlier
        notification (robot.move / robot.head) has been recorded."""
        self.fake.client.call("robot.mode")

    def moves(self):
        self.sync()
        return self.fake.moves()

    def methods(self):
        self.sync()
        return self.fake.methods()


class FollowFlowTest(AppHarness):
    def test_records_every_new_frame_in_any_mode(self):
        import os
        import tempfile
        from follow.sources import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            self.app.recorder = Recorder(os.path.join(tmp, "r.jsonl"))
            for _ in range(5):
                self.step(marker_size=.2)  # idle
            self.commands.put("follow")
            for _ in range(5):
                self.step(marker_size=.2)
            self.app.recorder.close()
            self.assertEqual(self.app.recorder.count, 10)

    def test_idle_sends_nothing(self):
        for _ in range(10):
            self.step(marker_size=.2)
        self.assertEqual(self.moves(), [])
        self.assertNotIn("robot.head", self.methods())

    def test_follow_far_marker_makes_one_bounded_pulse(self):
        self.commands.put("follow")
        self.step(marker_size=.2)
        self.assertEqual(self.app.cycle.phase, "settling", "zero must be acknowledged first")
        speeds, phases = [], []
        for _ in range(80):  # 8 s: room for two pulses
            out, sent = self.step(marker_size=.2)
            speeds.append(sent[0])
            phases.append(out.phase)
        self.assertIn("moving", phases)
        self.assertTrue(all(0 <= v <= 0.12 for v in speeds))
        self.assertGreater(max(speeds), 0)
        for move in self.moves():
            self.assertEqual(move["vy"], 0.0)
            self.assertEqual(move["vyaw"], 0.0)
            self.assertLessEqual(move["vx"], 0.12)
        # Split into pulses: each is at most move_seconds (2.5 s ≈ 25 ticks) of non-zero
        # commands, and consecutive pulses are separated by at least the 1 s settle.
        runs, gaps, current, idle = [], [], 0, 0
        for v in speeds:
            if v > 0:
                if current == 0 and runs:
                    gaps.append(idle)
                current, idle = current + 1, 0
            else:
                if current:
                    runs.append(current)
                current, idle = 0, idle + 1
        if current:
            runs.append(current)
        self.assertGreaterEqual(len(runs), 2, "a far marker keeps producing pulses")
        self.assertTrue(all(r <= 25 for r in runs), runs)
        self.assertTrue(all(g >= 10 for g in gaps), gaps)
        self.assertIn("robot.stop", self.methods())
        self.assertIn("robot.head", self.methods())

    def test_matching_size_never_moves(self):
        self.commands.put("follow")
        for _ in range(40):
            self.step(marker_size=.4)
        self.assertEqual(self.moves(), [])

    def test_no_video_never_moves(self):
        self.commands.put("follow")
        for _ in range(40):
            self.step()
        self.assertEqual(self.moves(), [])
        self.assertIn("視訊", self.app.problem)

    def test_marker_lost_mid_pulse_brakes(self):
        self.commands.put("follow")
        while self.app.cycle.phase != "moving":
            self.step(marker_size=.2)
        self.step(marker_size=.2)
        moves_before = len(self.moves())
        self.assertGreater(moves_before, 0)
        # The marker stays inside the tracker's 12 s memory, so a short loss does not brake by
        # itself; a gate failure must. Sit down (policy changes) mid-pulse:
        self.policy = "sit"
        out, sent = self.step()
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        self.assertIn("robot.stop", self.methods())
        self.assertNotEqual(self.app.cycle.phase, "moving")
        self.policy = "stand"
        for _ in range(5):
            out, sent = self.step()
            self.assertEqual(sent, (0.0, 0.0, 0.0), "must not resume on its own")
        self.assertEqual(len(self.moves()), moves_before)

    def test_fall_then_stood_up_does_not_resume(self):
        """Review #2: after a fall the app goes idle; standing it up again must not restart walking."""
        self.commands.put("follow")
        while self.app.cycle.phase != "moving":
            self.step(marker_size=.2)
        self.step(marker_size=.2)
        self.policy = "held"
        self.fake.push("robot.state", standing_state(policy="held", safety={"fallen": True, "limp": True}))
        self.assertTrue(wait_for(lambda: self.fake.client.state["safety"]["fallen"] is True))
        self.app.tick()
        self.guard.tick()
        self.assertEqual(self.app.mode, "idle")
        self.assertTrue(any("回到待命" in line for line in self.logs))
        count = len(self.moves())
        self.policy = "stand"  # someone stood it back up (follow.stand)
        for _ in range(40):
            self.step(marker_size=.2)
        self.assertEqual(len(self.moves()), count, "no walking without a new command")
        self.assertEqual(self.app.mode, "idle")

    def test_stale_state_does_resume(self):
        """Staleness is not a hard stop: it clears and the cycle restarts by itself."""
        self.commands.put("follow")
        for _ in range(5):
            self.step(marker_size=.2)
        self.clock.now += 0.3
        self.fake.client.state_at = self.clock.now - 2.0  # state 2 s old
        self.app.tick()
        self.assertEqual(self.app.mode, "follow")
        self.assertIn("過期", self.app.problem)
        for _ in range(40):
            self.step(marker_size=.2)
        self.assertGreater(max(m["vx"] for m in self.moves()), 0)

    def test_settle_measured_after_a_slow_stop_ack(self):
        """Review #4: a stop ack that takes 0.9 s must not eat into the 1 s settle."""
        def slow_stop(params):
            # The ack takes 0.9 s; meanwhile robotd keeps pushing state and the health thread
            # keeps polling, as on the robot.
            self.clock.now += 0.9
            self.fake.push("robot.state", standing_state(policy=self.policy))
            self.guard.health_at = self.clock.now
            return {"accepted": True}
        self.fake.handlers["robot.stop"] = slow_stop
        self.commands.put("follow")
        self.step(marker_size=.2)
        self.assertEqual(self.app.cycle.phase, "settling")
        self.assertGreaterEqual(self.app.cycle.settle_until, self.clock.now + 1.0 - 1e-9)
        self.assertGreaterEqual(self.app.last_ingested, self.clock.now - 1e-9)

    def test_target_lost_mid_pulse_brakes_at_once(self):
        """Review #7 / project rule: losing the target stops the pulse, not 2.5 s later."""
        self.commands.put("follow")
        while self.app.cycle.phase != "moving":
            self.step(marker_size=.2)
        self.step(marker_size=.2)
        self.step(marker_size=.2)
        out, sent = self.step()  # the next frame has no marker in it
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        self.assertNotEqual(self.app.cycle.phase, "moving")
        self.assertIn("robot.stop", self.methods())

    def test_voice_home_ignored_when_idle(self):
        """Review: a misheard "go home" must not spin an idle duck 180°; the key still works."""
        self.commands.put("home", source="voice")
        for _ in range(10):
            self.step()
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.moves(), [])
        self.assertTrue(any("請按 h" in line for line in self.logs))
        self.commands.put("home")  # keyboard
        self.step()
        self.assertEqual(self.app.mode, "home")

    def test_voice_home_accepted_mid_show(self):
        self.commands.put("follow", source="voice")
        self.step(marker_size=.4)
        self.commands.put("home", source="voice")
        self.step(marker_size=.4)
        self.assertEqual(self.app.mode, "home")

    def test_camera_dies_mid_pulse_brakes_at_once(self):
        """Review: no new frames while moving → brake within ~1.5 s, not at the 2.5 s deadline."""
        self.commands.put("follow")
        while self.app.cycle.phase != "moving":
            self.step(marker_size=.2)
        self.step(marker_size=.2)
        frozen = self.source.frame
        braked_after = None
        for i in range(1, 30):
            self.clock.now += 0.1
            self.push_state()
            self.guard.poll_health()
            self.source.frame = frozen  # worker died: the last frame never changes
            self.app.tick()
            sent = self.guard.tick()
            if sent == (0.0, 0.0, 0.0):
                braked_after = i * 0.1
                break
        self.assertIsNotNone(braked_after)
        self.assertLessEqual(braked_after, 1.6 + 1e-9)
        self.assertIn("沒有新畫面", self.app.problem)

    def test_source_error_blocks_motion(self):
        self.source.error = "偵測程式結束（1）"
        self.commands.put("follow")
        for _ in range(30):
            self.step(marker_size=.2)
        self.assertEqual(self.moves(), [])
        self.assertIn("相機程式停止", self.app.problem)

    def test_stop_command_mid_pulse(self):
        self.commands.put("follow")
        while self.app.cycle.phase != "moving":
            self.step(marker_size=.2)
        self.step(marker_size=.2)
        self.commands.put("stop")
        out, sent = self.step(marker_size=.2)
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        count = len(self.moves())
        for _ in range(20):
            self.step(marker_size=.2)
        self.assertEqual(len(self.moves()), count)

    def test_frame_from_before_the_stop_is_never_used(self):
        self.commands.put("follow")
        p = picture(0, 0, .2)
        p["boxes"][0]["id"] = 0
        self.source.show(p, self.clock.now)  # captured before the stop acknowledgement
        stale = self.source.frame
        for _ in range(30):
            self.clock.now += 0.1
            self.push_state()
            self.guard.poll_health()
            self.source.frame = Frame(captured_at=stale.captured_at, video_at=self.clock.now,
                                      detections=stale.detections)
            self.app.tick()
            self.guard.tick()
        self.assertEqual(self.moves(), [])

    def test_loop_stall_stops(self):
        self.commands.put("follow")
        while self.app.cycle.phase != "moving":
            self.step(marker_size=.2)
        self.step(marker_size=.2)
        out, sent = self.step(dt=0.6, marker_size=.2)
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        self.assertIn("停頓", self.app.problem)

    def test_gaze_moves_head_only(self):
        self.commands.put("gaze")
        for _ in range(10):
            self.step(marker_size=.2, x=.4)
        self.assertEqual(self.moves(), [])
        self.assertIn("robot.head", self.methods())

    def test_quit_mid_home_turn_stops(self):
        self.commands.put("home")
        for _ in range(15):
            self.step()
        self.assertEqual(self.app.mode, "home")
        self.assertIsNotNone(self.app.turn)
        self.commands.put("quit")
        out, sent = self.step()
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        self.assertIn("robot.stop", self.methods())

    def test_low_battery_blocks_follow(self):
        self.fake.health["battery"] = {"volts": 7.15, "percent": 40}
        self.commands.put("follow")
        for _ in range(40):
            self.step(marker_size=.2)
        self.assertEqual(self.moves(), [])
        self.assertTrue(any("請充電" in line for line in self.logs))


class TurningFlowTest(AppHarness):
    def run_until(self, predicate, limit=300, **world):
        for _ in range(limit):
            if predicate():
                return True
            if world:
                self.step_world(**world)
            else:
                self.step()
        return predicate()

    def test_home_turns_round_then_follows(self):
        self.commands.put("home")
        self.assertTrue(self.run_until(lambda: self.app.mode == "follow"))
        self.assertLess(abs(wrap(self.yaw - math.pi)), 0.12)
        moves = self.moves()
        self.assertTrue(moves)
        for move in moves:
            self.assertEqual(move["vx"], 0.0, "turning is in place")
            self.assertEqual(move["vy"], 0.0)
            self.assertGreaterEqual(move["vyaw"], 0.0, "home turns left")
            self.assertLessEqual(move["vyaw"], 0.40)
        self.assertIn("robot.stop", self.methods())
        self.assertTrue(any("回家轉身完成" in line for line in self.logs))

    def test_home_turn_head_returns_to_centre(self):
        self.push_state()
        self.fake.push("robot.state", standing_state(head=[0.0, 0.1, 0.4, 0.0], odom={"yaw": 0.0}))
        self.assertTrue(wait_for(lambda: self.fake.client.state["head"][2] == 0.4))
        self.commands.put("home")
        self.step()
        yaws = []
        for _ in range(30):
            self.step()
            yaws.append(self.guard.last_command["head"][2])
        self.assertTrue(all(b <= a + 1e-12 for a, b in zip(yaws, yaws[1:])), "head yaw only decreases")
        self.assertEqual(yaws[-1], 0.0)

    def test_home_without_rotation_fails_and_stops(self):
        self.sim_turn = 0.0  # commanded, but the heading never changes
        self.commands.put("home")
        self.step()
        self.assertEqual(self.app.mode, "home")
        self.assertTrue(self.run_until(lambda: self.app.mode == "idle", limit=60))
        self.assertGreaterEqual(self.clock.now - 1000.1, 2.0, "gives the turn stall_seconds first")
        self.assertTrue(any("沒有變化" in line for line in self.logs))
        self.assertIn("robot.stop", self.methods())
        count = len(self.moves())
        for _ in range(10):
            self.step()
        self.assertEqual(len(self.moves()), count)

    def test_home_without_odom_refuses(self):
        self.odom = False
        self.push_state()
        self.commands.put("home")
        self.step()
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.moves(), [])
        self.assertTrue(any("odom" in line for line in self.logs))

    def test_gate_failure_aborts_turn(self):
        self.commands.put("home")
        for _ in range(15):
            self.step()
        self.policy = "sit"
        out, sent = self.step()
        self.assertEqual(sent, (0.0, 0.0, 0.0))
        self.assertEqual(self.app.mode, "idle")
        self.assertIn("robot.stop", self.methods())

    def test_follow_aligns_body_to_off_axis_marker(self):
        bearing = -0.45  # marker 26° to the right, at the preferred distance (size .4)
        self.commands.put("follow")
        self.assertTrue(self.run_until(lambda: any("對正轉身完成" in line for line in self.logs),
                                       bearing=bearing, size=.4))
        turns = [m for m in self.moves() if m["vyaw"] != 0]
        self.assertTrue(turns)
        self.assertTrue(all(m["vyaw"] < 0 and m["vx"] == 0 for m in turns), "turns right, in place")
        self.assertLess(self.yaw, -0.2)
        for _ in range(60):
            self.step_world(bearing=bearing, size=.4)
        head = self.guard.last_command["head"][2]
        self.assertLess(abs(wrap(bearing - self.yaw - head)), 0.15, "camera ends on the marker")
        self.assertLess(abs(head), self.app.align_threshold + 0.05)

    def test_small_offset_does_not_turn(self):
        self.commands.put("follow")
        for _ in range(80):
            self.step_world(bearing=-0.15, size=.4)
        self.assertFalse(any(m["vyaw"] != 0 for m in self.moves()))


class DryRunTest(AppHarness):
    dry_run = True

    def test_dry_run_sends_nothing_that_moves(self):
        self.commands.put("follow")
        moved = False
        for _ in range(60):
            out, sent = self.step(marker_size=.2)
            moved = moved or sent[0] > 0
        self.assertTrue(moved, "dry run still computes the pulse")
        motor = {"robot.move", "robot.head", "robot.stop"}
        self.assertFalse(motor & set(self.methods()))
        self.assertIn("move", self.guard.last_command)


class SourcesTest(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(parse_command("f"), "follow")
        self.assertEqual(parse_command("Follow Me"), "follow")
        self.assertEqual(parse_command(""), "stop")
        self.assertEqual(parse_command("let's go home"), "home")
        self.assertIsNone(parse_command("moonwalk"))
        self.assertEqual(parse_command("d"), "dance")
        self.assertEqual(parse_command("Let's party"), "dance")
        # the desktop app's terminal over SSH can double a keystroke: "uu" is still one u
        self.assertEqual(parse_command("uu"), "stand")
        self.assertEqual(parse_command("uuu"), "stand")
        self.assertEqual(parse_command("22"), "roll")  # one press; the second Enter still confirms
        self.assertIsNone(parse_command("zz"))
        self.assertIsNone(parse_command("ud"))

    def test_command_queue(self):
        q = CommandQueue()
        q.put("follow")
        q.put("stop")
        self.assertEqual(q.drain(), ["follow", "stop"])
        self.assertEqual(q.drain(), [])
        with self.assertRaises(ValueError):
            q.put("moonwalk")

    def test_read_lines_eof_quits(self):
        q, logs = CommandQueue(), []
        q.read_lines(io.StringIO("f\nnonsense\n\n"), log=logs.append).join(1)
        self.assertEqual(q.drain(), ["follow", "stop", "quit"])
        self.assertEqual(len(logs), 1)

    def test_replay_source(self):
        clock = Clock()
        source = ReplaySource([{"t": 0.5, "detections": {"a": 1}}, {"t": 0.0, "detections": {"a": 0}}], clock)
        self.assertEqual(source.latest().detections, {"a": 0})
        clock.now += 0.6
        frame = source.latest()
        self.assertEqual(frame.detections, {"a": 1})
        self.assertEqual(frame.captured_at, 1000.5)
        self.assertFalse(source.live)

    def test_recorder_round_trip(self):
        import os
        import tempfile
        from follow.sources import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rec.jsonl")
            rec = Recorder(path)
            for t in (10.0, 10.0, 10.5, 10.4, 11.0):  # duplicates and out-of-order are skipped
                rec.write(Frame(captured_at=t, video_at=t, detections={"at": t}))
            rec.write(None)
            rec.close()
            self.assertEqual(rec.count, 3)
            clock = Clock()
            replay = ReplaySource.from_file(path, clock)
            self.assertEqual(replay.latest().detections, {"at": 10.0})
            clock.now += 1.0
            self.assertEqual(replay.latest().detections, {"at": 11.0})

    def test_live_is_refused(self):
        self.assertEqual(app_module.main(["--live"]), 2)


if __name__ == "__main__":
    unittest.main()
