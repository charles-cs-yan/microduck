"""Sit / get up / crouch ("趴下") / one-shot skills, through FollowApp and MotionGuard."""

import unittest

from follow.posture import CROUCH, TILT_LIMIT, Z_RANGE, clamp_pose
from follow.tests.fake_robotd import standing_state
from follow.tests.test_app import AppHarness
from follow.tests.test_robotd_client import wait_for


class ClampPoseTest(unittest.TestCase):
    def test_trained_ranges(self):
        self.assertEqual(clamp_pose(-1, -1, 1), (Z_RANGE[0], -TILT_LIMIT, TILT_LIMIT))
        self.assertEqual(clamp_pose(1, 0, 0)[0], Z_RANGE[1])
        self.assertEqual(clamp_pose(float("nan"), None, "x"), (0.0, 0.0, 0.0))
        self.assertTrue(Z_RANGE[0] <= CROUCH["z"] <= Z_RANGE[1] and abs(CROUCH["pitch"]) <= TILT_LIMIT)


class BringUpProblemTest(unittest.TestCase):
    """Each branch of posture.bring_up_problem, in a state where ONLY that branch decides
    (the duck is down, so "it fell" alone would make a bring-up look reasonable)."""

    def problem(self, policy, fallen=True, limp=False, allow_protection=False, **health):
        from follow.posture import bring_up_problem
        from follow.tests.fake_robotd import healthy_health
        state = standing_state(policy=policy, safety={"fallen": fallen, "limp": limp})
        return bring_up_problem(state, 0.1, healthy_health(**health), 0.1, allow_protection=allow_protection)

    def test_allowed_from_held_or_down(self):
        self.assertEqual(self.problem("held", fallen=False), "")
        self.assertEqual(self.problem("held"), "")
        self.assertEqual(self.problem("stand", fallen=True), "", "standing label but fallen: bring-up is the recovery")

    def test_busy_even_when_down(self):
        for policy in ("roulade", "ground_pick", "kick_left", "rise", "standup"):
            self.assertIn("正在", self.problem(policy), policy)  # mid-roll the IMU says fallen

    def test_sitting_even_when_limp(self):
        self.assertIn("sit_toggle", self.problem("sit", fallen=False, limp=True))

    def test_protection_only_for_the_operator_tool(self):
        self.assertIn("保護", self.problem("protection"))
        self.assertEqual(self.problem("protection", allow_protection=True), "")

    def test_battery_and_imu(self):
        self.assertIn("電池", self.problem("held", battery={"volts": 7.4, "percent": 30}))
        self.assertIn("IMU", self.problem("held", imu={"ready": False, "consecutive_stale_blocks": 0}))


class PostureHarness(AppHarness):
    forbidden = {"robot.relax", "robot.init"}  # posture commands may send do/pose/enable, never these

    def sent(self, method):
        self.sync()
        return [m["params"] for m in self.fake.received if m.get("method") == method]

    def set_state(self, policy, fallen=False, limp=False):
        self.policy = policy
        self.fake.push("robot.state", standing_state(policy=policy, safety={"fallen": fallen, "limp": limp}))
        self.assertTrue(wait_for(lambda: (self.fake.client.state or {}).get("policy") == policy))

    def step_as(self, policy, fallen=False, limp=False, dt=0.1):
        """One tick with a given robot state (the harness's push_state only knows `policy`)."""
        self.clock.now += dt
        self.fake.push("robot.state", standing_state(policy=policy, safety={"fallen": fallen, "limp": limp}))
        self.assertTrue(wait_for(lambda: self.fake.client.state_at == self.clock.now))
        self.guard.poll_health()
        self.app.tick()
        return self.guard.tick()


class SitTest(PostureHarness):
    def test_sit_then_get_up(self):
        self.commands.put("sit")
        self.step()
        self.assertEqual(self.app.mode, "sit")
        self.assertEqual(self.sent("robot.do"), [{"skill": "sit_toggle"}])
        methods = self.methods()
        self.assertLess(methods.index("robot.stop"), methods.index("robot.do"), "stopped before sitting")
        for _ in range(5):
            self.step_as("sit")
        self.assertEqual(self.app.mode, "sit", "sitting is not a hard problem in sit mode")
        self.commands.put("stand", source="voice")  # GET UP: allowed from sitting
        self.step_as("sit")
        self.assertEqual(self.app.mode, "skill")
        self.assertEqual(self.sent("robot.do"), [{"skill": "sit_toggle"}] * 2)
        for policy in ("rise", "rise", "stand", "stand", "stand", "stand", "stand", "stand"):
            self.step_as(policy)
        self.assertEqual(self.app.mode, "idle")
        self.assertTrue(any("站穩了" in line for line in self.logs))
        self.assertEqual([m for m in self.moves() if m["vx"] or m["vyaw"]], [], "never walks")

    def test_double_sit_never_toggles_back(self):
        self.commands.put("sit")
        self.step()
        self.commands.put("sit")  # pressed again before the policy reported "sit"
        self.step()
        self.assertEqual(len(self.sent("robot.do")), 1)

    def test_sit_refused_when_not_standing(self):
        self.set_state("held")
        self.commands.put("sit")
        self.step_as("held")
        self.assertEqual(self.sent("robot.do"), [])
        self.assertEqual(self.app.mode, "idle")

    def test_get_up_from_sit_needs_battery(self):
        self.fake.health["battery"] = {"volts": 7.35, "percent": 30}
        self.guard.poll_health()
        self.set_state("sit")
        self.commands.put("stand")
        self.step_as("sit")
        self.assertEqual(self.sent("robot.do"), [])
        self.assertTrue(any("先充電" in line for line in self.logs))

    def test_fall_while_sitting_goes_idle(self):
        self.commands.put("sit")
        self.step()
        self.step_as("sit")
        self.step_as("held", fallen=True, limp=True)
        self.assertEqual(self.app.mode, "idle")


class BringUpTest(PostureHarness):
    def test_from_the_floor_needs_two_presses_and_the_keyboard(self):
        self.set_state("held", fallen=True, limp=True)
        self.commands.put("stand", source="voice")
        self.step_as("held", fallen=True, limp=True)
        self.assertEqual(self.sent("robot.enable"), [], "voice never lifts the duck off the floor")
        self.commands.put("stand")
        self.step_as("held", fallen=True, limp=True)
        self.assertEqual(self.sent("robot.enable"), [], "first press only asks for confirmation")
        self.commands.put("stand")
        self.step_as("held", fallen=True, limp=True)
        self.assertEqual(self.sent("robot.enable"), [{"on": True, "toggle": False, "stand_up": True}])
        self.assertEqual(self.app.mode, "skill")
        for policy, fallen in (("standup", True), ("standup", False), ("stand", False), ("stand", False),
                               ("stand", False), ("stand", False), ("stand", False), ("stand", False)):
            self.step_as(policy, fallen=fallen)
        self.assertEqual(self.app.mode, "idle")

    def test_presses_too_far_apart_do_not_confirm(self):
        self.set_state("held")
        self.commands.put("stand")
        self.step_as("held")
        self.clock.now += 3.5
        self.commands.put("stand")
        self.step_as("held")
        self.assertEqual(self.sent("robot.enable"), [])

    def test_bring_up_timeout_reports(self):
        self.set_state("held")
        for _ in range(2):
            self.commands.put("stand")
            self.step_as("held")
        for _ in range(220):
            self.step_as("held")
            if self.app.mode == "idle":
                break
        self.assertEqual(self.app.mode, "idle")
        self.assertTrue(any("還沒站穩" in line for line in self.logs))


class CrouchTest(PostureHarness):
    def test_crouch_glides_down_holds_and_releases(self):
        self.commands.put("crouch")
        for _ in range(25):
            self.step()
        poses = [p for p in self.sent("robot.pose") if p.get("active")]
        self.assertGreater(len(poses), 10)
        zs = [p["z"] for p in poses]
        self.assertTrue(all(b <= a + 1e-12 for a, b in zip(zs, zs[1:])), "glides down, never jumps up")
        self.assertAlmostEqual(zs[-1], CROUCH["z"])
        self.assertTrue(all(Z_RANGE[0] <= z <= Z_RANGE[1] for z in zs))
        self.assertGreater(zs[0], CROUCH["z"] / 2, "starts near nominal, not at full crouch")
        self.commands.put("stop")
        self.step()
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False, "leaving 趴下 restores the body")
        self.assertEqual([m for m in self.moves() if m["vx"] or m["vyaw"]], [])

    def test_crouch_refused_unless_standing(self):
        self.set_state("sit")
        self.commands.put("crouch")
        self.step_as("sit")
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(self.sent("robot.pose"), [])

    def test_fall_in_crouch_goes_idle_and_stops_posing(self):
        self.commands.put("crouch")
        for _ in range(5):
            self.step()
        self.step_as("held", fallen=True, limp=True)
        self.assertEqual(self.app.mode, "idle")
        count = len([p for p in self.sent("robot.pose") if p.get("active")])
        for _ in range(5):
            self.step_as("held", fallen=True, limp=True)
        self.assertEqual(len([p for p in self.sent("robot.pose") if p.get("active")]), count)

    def test_close_restores_body(self):
        self.commands.put("crouch")
        for _ in range(3):
            self.step()
        self.guard.close()
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)


class SkillTest(PostureHarness):
    def test_pick_runs_once_and_waits_to_stand(self):
        self.commands.put("pick")
        self.step()
        self.assertEqual(self.sent("robot.do"), [{"skill": "ground_pick"}])
        for policy in ("ground_pick",) * 10 + ("stand",) * 8:
            self.step_as(policy)
        self.assertEqual(self.app.mode, "idle")
        self.assertEqual(len(self.sent("robot.do")), 1)

    def test_roll_needs_confirmation_and_tolerates_fallen_mid_roll(self):
        self.commands.put("roll")
        self.step()
        self.assertEqual(self.sent("robot.do"), [])
        self.commands.put("roll")
        self.step()
        self.assertEqual(self.sent("robot.do"), [{"skill": "roulade"}])
        self.step_as("roulade", fallen=True)  # mid-roll the IMU says "fallen": not a stop reason
        self.assertEqual(self.app.mode, "skill")
        for _ in range(8):
            self.step_as("stand")
        self.assertEqual(self.app.mode, "idle")

    def test_voice_cannot_trigger_skills(self):
        for command in ("pick", "roll", "kick_left"):
            self.commands.put(command, source="voice")
            self.commands.put(command, source="voice")
        for _ in range(3):
            self.step()
        self.assertEqual(self.sent("robot.do"), [])

    def test_refused_kick_is_reported(self):
        self.fake.handlers["robot.do"] = lambda p: {"accepted": False, "reason": "kick networks not loaded"}
        self.commands.put("kick_left")
        self.step()
        self.commands.put("kick_left")
        self.step()
        self.assertEqual(self.app.mode, "idle")
        self.assertTrue(any("kick networks not loaded" in line for line in self.logs))

    def test_skill_refused_on_low_battery(self):
        self.fake.health["battery"] = {"volts": 7.55, "percent": 40}
        self.guard.poll_health()
        self.commands.put("pick")
        self.step()
        self.assertEqual(self.sent("robot.do"), [])

    def test_no_new_posture_command_while_a_skill_runs(self):
        """Right after robot.do is accepted robotd may still report "stand" for a tick or two;
        a second command in that window must not be sent (sit_toggle would even undo a sit)."""
        self.commands.put("pick")
        self.step()
        self.commands.put("sit")   # policy still "stand": only the skill-mode guard stops this
        self.step()
        self.commands.put("pick")
        self.step()
        self.assertEqual(self.sent("robot.do"), [{"skill": "ground_pick"}])
        self.assertTrue(any("還在進行" in line for line in self.logs))

    def test_protection_after_skill(self):
        self.commands.put("pick")
        self.step()
        self.step_as("ground_pick")
        self.step_as("protection")
        self.assertEqual(self.app.mode, "idle")
        self.assertTrue(any("保護" in line for line in self.logs))


class ReviewRegressionTest(PostureHarness):
    """Third review (posture): each test is one reproduced scenario."""

    def test_a_stop_during_a_skill_does_not_unlock_posture_commands(self):
        self.commands.put("pick")
        self.step()
        self.step_as("ground_pick")
        self.commands.put("stop")          # leaves skill mode, but robotd keeps picking
        self.step_as("ground_pick")
        for _ in range(2):
            self.commands.put("stand")
            self.step_as("ground_pick")
        self.assertEqual(self.sent("robot.enable"), [], "no bring-up while robotd is mid-skill")
        self.assertTrue(any("robotd 會把它做完" in line for line in self.logs))

    def test_b_confirmation_must_be_two_presses_in_a_row(self):
        for between in ("stop", "follow"):
            self.commands.put("roll")
            self.step()
            self.commands.put(between)
            self.step(marker_size=.4)
            self.commands.put("roll")
            self.step(marker_size=.4)
            self.commands.put("stop")
            self.step()
        self.assertEqual(self.sent("robot.do"), [])

    def test_c_get_up_refused_when_latched(self):
        self.set_state("sit")
        self.guard.failed = "內部錯誤（測試）"
        self.commands.put("stand", source="voice")
        self.step_as("sit")
        self.assertEqual(self.sent("robot.do"), [])

    def test_d_no_bring_up_while_rising_or_protected(self):
        for policy, fallen in (("rise", False), ("standup", False), ("protection", False), ("protection", True)):
            self.set_state(policy, fallen=fallen)
            for _ in range(2):
                self.commands.put("stand")
                self.step_as(policy, fallen=fallen)
        self.assertEqual(self.sent("robot.enable"), [])
        self.assertTrue(any("follow.stand" in line for line in self.logs), "protection points at the operator tool")

    def test_e_nothing_else_while_sitting_down(self):
        self.commands.put("sit")
        self.step()                        # robotd still says "stand" this cycle
        for command in ("pick", "crouch", "stand"):
            self.commands.put(command)
            self.step()
        self.assertEqual(self.sent("robot.do"), [{"skill": "sit_toggle"}])
        self.assertEqual(self.app.mode, "sit", "still tracking the sit")

    def test_f_get_up_refused_on_stale_state(self):
        self.set_state("sit")
        self.clock.now += 2.0
        self.guard.poll_health()
        self.commands.put("stand", source="voice")
        self.app.tick()
        self.assertEqual(self.sent("robot.do"), [])

    def test_g_release_wins_against_a_concurrent_tick(self):
        self.commands.put("crouch")
        for _ in range(3):
            self.step()
        original, fired = self.guard.head_problem, []

        def interleaved():
            if not fired:
                fired.append(1)
                self.guard.release_pose()  # main thread leaves 趴下 between the copy and the send
            return original()

        self.guard.head_problem = interleaved
        self.guard.tick()
        self.guard.head_problem = original
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)

    def test_pose_not_refreshed_is_actively_released(self):
        """robotd's pose has no deadman: a stalled app must not leave the duck crouched."""
        self.commands.put("crouch")
        for _ in range(3):
            self.step()
        self.clock.now += 1.0             # the app stopped refreshing set_pose
        self.push_state()
        self.guard.tick()
        self.assertEqual(self.sent("robot.pose")[-1]["active"], False)

    def test_7_no_posture_while_walking_and_no_posture_voice_mid_follow(self):
        self.policy = "walk"
        self.fake.push("robot.state", dict(standing_state(policy="walk"),
                                           move={"requested": [0.1, 0, 0], "applied": [0.1, 0, 0]}))
        self.assertTrue(wait_for(lambda: (self.fake.client.state or {}).get("policy") == "walk"))
        self.commands.put("pick")
        self.app.tick()
        self.assertEqual(self.sent("robot.do"), [], "still walking: refused")
        self.set_state("stand")
        self.commands.put("follow")
        self.step(marker_size=.4)
        for command in ("sit", "crouch"):
            self.commands.put(command, source="voice")  # host tells the audience to "sit down"
            self.step(marker_size=.4)
        self.assertEqual(self.sent("robot.do"), [])
        self.assertEqual(self.app.mode, "follow")


class DryRunPostureTest(PostureHarness):
    dry_run = True

    def test_dry_run_sends_no_posture(self):
        for command in ("sit", "crouch", "pick"):
            self.commands.put(command)
            self.step()
            self.commands.put("stop")
            self.step()
        self.assertFalse({"robot.do", "robot.pose", "robot.enable"} & set(self.methods()))


if __name__ == "__main__":
    unittest.main()
