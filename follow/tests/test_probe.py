import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest

from follow import probe
from follow.tests.fake_robotd import healthy_health, standing_state

READ_ONLY = {"hello", "robot.mode", "robot.subscribe", "robot.health"}


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs AF_UNIX (Linux / WSL / the board)")
class ProbeTest(unittest.TestCase):
    def test_probe_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "robotd.sock")
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            seen = []

            def serve():
                conn, _ = server.accept()
                with conn, conn.makefile("rb") as reader:
                    for line in reader:
                        message = json.loads(line)
                        seen.append(message["method"])
                        result = {"hello": {"api_version": 18, "daemon_version": "0.10.0"},
                                  "robot.mode": {"mode": "walk"},
                                  "robot.subscribe": {"hz": 10},
                                  "robot.health": healthy_health()}.get(message["method"], {})
                        out = [{"jsonrpc": "2.0", "method": "robot.state", "params": standing_state()},
                               {"jsonrpc": "2.0", "id": message["id"], "result": result}]
                        try:
                            conn.sendall("".join(json.dumps(m) + "\n" for m in out).encode())
                        except OSError:
                            return

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(probe.main(["--socket", path, "--seconds", "0.6", "--health-hz", "10"]), 0)
            thread.join(2)
            server.close()
        self.assertTrue(set(seen) <= READ_ONLY, set(seen) - READ_ONLY)
        self.assertIn("可以移動", output.getvalue())
        self.assertIn("電池 8.20", output.getvalue())


if __name__ == "__main__":
    unittest.main()
