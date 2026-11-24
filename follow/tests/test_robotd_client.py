import json
import time
import unittest

from follow.robotd_client import Disconnected, Refused, RobotdError
from follow.tests.fake_robotd import FakeRobotd, standing_state


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRobotd()
        self.client = self.fake.client

    def tearDown(self):
        self.fake.close()

    def test_hello_sends_v18(self):
        self.assertEqual(self.client.hello(), {"api_version": 18})
        hello = self.fake.received[0]
        self.assertEqual(hello["method"], "hello")
        self.assertEqual(hello["params"], {"api_version": 18})
        self.assertEqual(hello["jsonrpc"], "2.0")

    def test_notify_has_no_id(self):
        self.client.notify("robot.move", {"vx": 0.1, "vy": 0.0, "vyaw": 0.0})
        self.assertTrue(wait_for(lambda: self.fake.moves()))
        self.assertNotIn("id", self.fake.received[0])

    def test_refusal_raises_unless_allowed(self):
        self.fake.handlers["robot.do"] = lambda p: {"accepted": False, "reason": "fallen"}
        with self.assertRaises(Refused) as caught:
            self.client.call("robot.do", {"skill": "sit_toggle"})
        self.assertEqual(caught.exception.reason, "fallen")
        result = self.client.call("robot.do", {"skill": "sit_toggle"}, allow_refusal=True)
        self.assertFalse(result["accepted"])

    def test_jsonrpc_error_raises(self):
        with self.assertRaises(RobotdError):
            self.client.call("robot.nonexistent", {})

    def test_timeout(self):
        self.fake.silent.add("robot.health")
        with self.assertRaises(RobotdError):
            self.client.health(timeout=0.1)

    def test_state_pushes_interleaved_with_responses(self):
        self.fake.handlers["robot.health"] = lambda p: (self.fake.push("robot.state", standing_state()), {"healthy": True})[1]
        self.assertEqual(self.client.health(), {"healthy": True})
        self.assertTrue(wait_for(lambda: self.client.state is not None))
        self.assertEqual(self.client.state["policy"], "stand")
        self.assertLess(self.client.state_age(), 1.0)

    def test_corrupt_line_is_ignored(self):
        self.fake.push_raw(b"not json\n")
        self.fake.push("robot.state", standing_state(policy="walk"))
        self.assertTrue(wait_for(lambda: self.client.state is not None))
        self.assertEqual(self.client.mode(), "walk")

    def test_partial_lines_reassembled(self):
        line = (json.dumps({"jsonrpc": "2.0", "method": "robot.state", "params": standing_state()}) + "\n").encode()
        self.fake.push_raw(line[:10])
        time.sleep(0.02)
        self.fake.push_raw(line[10:])
        self.assertTrue(wait_for(lambda: self.client.state is not None))

    def test_stop_never_raises(self):
        self.assertTrue(self.client.stop())
        self.fake.silent.add("robot.stop")
        self.assertFalse(self.client.stop(timeout=0.1))
        self.fake.hangup()
        self.assertTrue(wait_for(lambda: not self.client.connected))
        self.assertFalse(self.client.stop())

    def test_disconnect_wakes_a_waiting_call(self):
        import threading
        self.fake.silent.add("robot.health")
        errors = []

        def waiter():
            try:
                self.client.health(timeout=2.0)
            except Exception as error:  # noqa: BLE001 - recording the type
                errors.append(error)

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(wait_for(lambda: "robot.health" in self.fake.methods()))
        started = time.monotonic()
        self.fake.hangup()
        thread.join(2.5)
        self.assertLess(time.monotonic() - started, 1.0, "a hang-up must not wait for the timeout")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Disconnected)

    def test_hangup_releases_client_socket(self):
        self.fake.hangup()
        self.assertTrue(wait_for(lambda: not self.client.connected))
        self.assertTrue(wait_for(lambda: self.client._sock.fileno() == -1), "fd must be closed after a hang-up")

    def test_disconnect_fails_pending_and_sends(self):
        self.fake.hangup()
        self.assertTrue(wait_for(lambda: not self.client.connected))
        with self.assertRaises(Disconnected):
            self.client.health(timeout=1.0)
        with self.assertRaises(Disconnected):
            self.client.notify("robot.move", {"vx": 0, "vy": 0, "vyaw": 0})


if __name__ == "__main__":
    unittest.main()
