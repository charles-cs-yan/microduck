import unittest

from follow import stand
from follow.tests.fake_robotd import FakeRobotd, healthy_health, standing_state
from follow.tests.test_robotd_client import wait_for


class StandToolTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRobotd()
        self.fake.handlers["robot.enable"] = lambda p: {"accepted": True}
        self.logs = []
        self.script = []  # policies robotd reports after each sleep

    def tearDown(self):
        self.fake.close()

    def push(self, **overrides):
        self.fake.push("robot.state", standing_state(**overrides))
        target = overrides.get("policy", "stand")
        self.assertTrue(wait_for(lambda: (self.fake.client.state or {}).get("policy") == target))

    def sleeper(self, seconds):
        if self.script:
            policy, fallen, limp = self.script.pop(0)
            self.push(policy=policy, safety={"fallen": fallen, "limp": limp})

    def run_tool(self, answer="yes", timeout=5.0):
        clock = [0.0]

        def tick():
            clock[0] += 0.1
            return clock[0]

        def sleep(seconds):
            self.sleeper(seconds)

        return stand.run(self.fake.client, confirm=lambda prompt: answer, timeout=timeout,
                         clock=tick, sleep=sleep, log=self.logs.append)

    def sent(self, method):
        self.fake.client.call("robot.mode")  # barrier
        return [m for m in self.fake.received if m.get("method") == method]

    def test_already_standing_sends_nothing(self):
        self.push(policy="walk")
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual(self.sent("robot.enable"), [])

    def test_fallen_duck_stands_after_yes(self):
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.script = [("standup", True, False), ("standup", False, False), ("stand", False, False)]
        self.assertEqual(self.run_tool(), 0)
        (enable,) = self.sent("robot.enable")
        self.assertEqual(enable["params"], {"on": True, "toggle": False, "stand_up": True})
        methods = [m["method"] for m in self.fake.received]
        self.assertLess(methods.index("robot.stop"), methods.index("robot.enable"), "stop first")
        self.assertTrue(any("站起來了" in l for l in self.logs))

    def test_no_yes_no_request(self):
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.assertEqual(self.run_tool(answer="y"), 1)
        self.assertEqual(self.sent("robot.enable"), [])
        self.assertEqual(self.sent("robot.stop"), [])

    def test_refuses_on_low_battery(self):
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.fake.health = healthy_health(battery={"volts": 7.15, "percent": 30})
        self.assertEqual(self.run_tool(), 2)
        self.assertEqual(self.sent("robot.enable"), [])

    def test_refuses_with_bad_imu_or_while_rising(self):
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.fake.health = healthy_health(imu={"ready": False, "consecutive_stale_blocks": 0})
        self.assertEqual(self.run_tool(), 2)
        self.fake.health = healthy_health()
        self.push(policy="standup", safety={"fallen": True, "limp": False})
        self.assertEqual(self.run_tool(), 2)
        self.assertEqual(self.sent("robot.enable"), [])

    def test_refusal_is_reported_and_never_retried(self):
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.fake.handlers["robot.enable"] = lambda p: {"accepted": False, "reason": "overcurrent"}
        self.assertEqual(self.run_tool(), 2)
        self.assertEqual(len(self.sent("robot.enable")), 1)
        self.assertTrue(any("overcurrent" in l for l in self.logs))

    def install_old_robotd(self):
        """The board's 10-23 robotd: EnableParams has no stand_up and denies unknown fields."""
        original = self.fake._send

        def handler(params):
            if "stand_up" in params:
                raise LookupError
            return {"accepted": True, "reason": "enabled — driving"}

        def send(message):
            if message.get("result") is LookupError:
                message = {"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32602, "message": "invalid params: unknown field `stand_up`, expected `on` or `toggle`"}}
            original(message)

        def safe_handler(params):
            try:
                return handler(params)
            except LookupError:
                return LookupError

        self.fake.handlers["robot.enable"] = safe_handler
        self.fake._send = send

    def test_old_robotd_without_stand_up(self):
        self.install_old_robotd()
        self.push(policy="held", safety={"fallen": False, "limp": False})
        self.script = [("held", False, False), ("stand", False, False)]
        self.assertEqual(self.run_tool(), 0)
        enables = [m["params"] for m in self.sent("robot.enable")]
        self.assertEqual(enables, [{"on": True, "toggle": False, "stand_up": True}, {"on": True, "toggle": False}])
        self.assertTrue(any("不認得 stand_up" in l for l in self.logs))

    def test_other_errors_are_not_retried(self):
        self.fake.handlers["robot.enable"] = None  # → JSON-RPC "method not found"
        self.push(policy="held", safety={"fallen": False, "limp": False})
        self.assertEqual(self.run_tool(), 2)
        self.assertEqual(len(self.sent("robot.enable")), 1)

    def test_review_8_refuses_sitting_and_busy(self):
        """stand.py must not lift a sitting duck with stand_up/enable (that is sit_toggle's job)
        nor interrupt a running skill."""
        for policy in ("sit", "roulade", "ground_pick", "rise", "standup"):
            self.push(policy=policy, safety={"fallen": False, "limp": False})
            self.assertEqual(self.run_tool(), 2, policy)
        self.assertEqual(self.sent("robot.enable"), [])

    def test_held_counts_as_not_standing(self):
        self.assertFalse(stand.standing(standing_state(policy="held")))

    def test_protection_and_timeout_never_relax(self):
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.script = [("standup", True, False), ("protection", True, False)]
        self.assertEqual(self.run_tool(), 3)
        self.push(policy="held", safety={"fallen": True, "limp": True})
        self.script = [("standup", True, False)] * 3
        self.assertEqual(self.run_tool(timeout=2.0), 3)
        self.assertEqual(self.sent("robot.relax"), [], "the tool never collapses the duck")


if __name__ == "__main__":
    unittest.main()
