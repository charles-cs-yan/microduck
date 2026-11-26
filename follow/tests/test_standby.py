"""--standby: boot standby — keys from a FIFO, nothing armed until "yes", link loss queues a stop."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from follow import app as app_mod
from follow.app import KeyPipe, start_command_pipe
from follow.sources import CommandQueue


class KeyPipeFeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = []
        self.keys = KeyPipe(Path(self.tmp.name) / "keys", say=self.logs.append)
        self.commands = CommandQueue()
        self.keys.attach(self.commands)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_before_yes(self):
        for line in ("f", "u", "", "d", "Yes please"):
            self.keys.feed(line)
        self.assertFalse(self.keys.armed.is_set())
        self.assertEqual(self.commands.drain_items(), [])
        self.assertTrue(all("還沒解鎖" in m for m in self.logs))

    def test_yes_arms_then_keys_are_keyboard_commands(self):
        self.keys.feed("yes")
        self.assertTrue(self.keys.armed.is_set())
        self.keys.feed("f")
        self.keys.feed("")          # Enter = stop
        self.keys.feed("h")
        self.assertEqual(self.commands.drain_items(),
                         [("follow", "keyboard"), ("stop", "keyboard"), ("home", "keyboard")])

    def test_yes_again_and_unknown_are_ignored(self):
        self.keys.feed("yes")
        self.keys.feed("yes")
        self.keys.feed("zz-top")
        self.assertEqual(self.commands.drain_items(), [])

    def test_console_drop_stops_only_once_armed(self):
        self.keys.closed()
        self.assertEqual(self.commands.drain_items(), [])
        self.keys.feed("yes")
        self.keys.closed()
        self.assertEqual(self.commands.drain_items(), [("stop", "keyboard")])


@unittest.skipUnless(hasattr(os, "mkfifo"), "needs POSIX FIFOs")
class PipeEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for(self, fn, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = fn()
            if got:
                return got
            time.sleep(0.02)
        self.fail("timed out")

    def test_key_fifo_arm_command_and_drop(self):
        path = Path(self.tmp.name) / "keys"
        keys = KeyPipe(path, say=lambda m: None).start()
        commands = CommandQueue()
        keys.attach(commands)
        with open(path, "w") as w:
            w.write("f\nyes\nf\n")
        self.assertTrue(keys.armed.wait(3))
        items = []
        self.wait_for(lambda: items.extend(commands.drain_items()) or len(items) >= 2)
        self.assertEqual(items[:2], [("follow", "keyboard"), ("stop", "keyboard")])  # writer closed → stop

    def test_voice_fifo_stop_on_eof(self):
        path = Path(self.tmp.name) / "voice"
        commands = CommandQueue()
        start_command_pipe(path, commands, say=lambda m: None, stop_on_eof=True)
        self.wait_for(lambda: path.exists())
        with open(path, "w") as w:
            w.write("dance\n")
        items = []
        self.wait_for(lambda: items.extend(commands.drain_items()) or len(items) >= 2)
        self.assertEqual(items[:2], [("dance", "voice"), ("stop", "keyboard")])

    def test_not_a_fifo_refused(self):
        path = Path(self.tmp.name) / "plain"
        path.write_text("")
        with self.assertRaises(SystemExit):
            KeyPipe(path, say=lambda m: None)


class StandbyArgsTest(unittest.TestCase):
    def test_standby_needs_scripted_and_key_pipe(self):
        for argv in (["--standby"], ["--standby", "--scripted"], ["--standby", "--key-pipe", "/tmp/x"],
                     ["--scripted", "--key-pipe", "/tmp/x"]):
            with self.assertRaises(SystemExit, msg=argv):
                app_mod.main(argv)


if __name__ == "__main__":
    unittest.main()
