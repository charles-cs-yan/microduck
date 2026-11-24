import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from follow.sources import CommandQueue
from follow.speech import VoiceCommands
from follow.tests.test_robotd_client import wait_for


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


def line(kind, **fields):
    return json.dumps({"kind": kind, "t": 0, **fields})


class VoiceParsingTest(unittest.TestCase):
    def setUp(self):
        self.clock, self.logs, self.queue = Clock(), [], CommandQueue()
        self.voice = VoiceCommands(self.queue, clock=self.clock, log=self.logs.append)

    def test_labels_map_to_commands(self):
        for label, command in (("follow", "follow"), ("home", "home"), ("STOP", "stop"), ("gaze", "gaze")):
            self.clock.now += 5
            self.assertEqual(self.voice.handle_line(line("keyword", label=label)), command)
        self.assertEqual(self.queue.drain(), ["follow", "home", "stop", "gaze"])

    def test_cooldown_but_stop_always_passes(self):
        self.assertEqual(self.voice.handle_line(line("keyword", label="follow")), "follow")
        self.clock.now += 0.5
        self.assertIsNone(self.voice.handle_line(line("keyword", label="follow")))
        self.assertEqual(self.voice.handle_line(line("keyword", label="stop")), "stop")
        self.assertEqual(self.voice.handle_line(line("keyword", label="stop")), "stop")
        self.clock.now += 2.1
        self.assertEqual(self.voice.handle_line(line("keyword", label="follow")), "follow")

    def test_garbage_and_unknown_ignored(self):
        for bad in ("not json", "[1]", line("keyword", label="moonwalk"), line("keyword")):
            self.assertIsNone(self.voice.handle_line(bad))
        self.assertEqual(self.queue.drain(), [])

    def test_status(self):
        self.assertEqual(self.voice.status(), "語音載入中")
        self.voice.handle_line(line("ready", model="m"))
        self.assertEqual(self.voice.status(), "語音正常")
        self.clock.now += 1.5
        self.assertEqual(self.voice.status(), "語音沒有音訊心跳")
        self.voice.handle_line(line("heartbeat"))
        self.assertEqual(self.voice.status(), "語音正常")
        self.voice.handle_line(line("error", message="mic busy"))
        self.assertIn("mic busy", self.voice.status())


class VoiceProcessTest(unittest.TestCase):
    def test_reads_a_child_process(self):
        script = textwrap.dedent("""
            import json, time
            for msg in ({"kind": "ready", "model": "m"}, {"kind": "heartbeat"},
                        {"kind": "keyword", "label": "follow"}, {"kind": "keyword", "label": "stop"}):
                print(json.dumps(msg), flush=True)
            time.sleep(0.2)
        """)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake_listen.py"
            path.write_text(script, encoding="utf-8")
            queue, logs = CommandQueue(), []
            voice = VoiceCommands(queue, log=logs.append).start(sys.executable, script=path)
            try:
                self.assertTrue(wait_for(lambda: len(voice.heard) == 2, timeout=5))
                self.assertTrue(wait_for(lambda: "結束" in voice.status(), timeout=5))
            finally:
                voice.close()
        self.assertEqual(queue.drain(), ["follow", "stop"])

    def test_close_terminates_a_running_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forever.py"
            path.write_text("import time\nwhile True: time.sleep(0.1)\n", encoding="utf-8")
            voice = VoiceCommands(CommandQueue(), log=lambda s: None).start(sys.executable, script=path)
            started = time.monotonic()
            voice.close()
            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNotNone(voice._process.poll())


class KeywordFileTest(unittest.TestCase):
    def test_every_keyword_maps_to_a_command(self):
        from follow.speech import LABELS
        raw = (Path(__file__).resolve().parents[1] / "voice" / "keywords_en.txt").read_text(encoding="utf-8")
        for entry in filter(None, (l.strip() for l in raw.splitlines())):
            phrase, label = entry.rsplit("@", 1)
            self.assertIn(label.strip(), LABELS, entry)
            self.assertEqual(phrase.strip(), phrase.strip().upper(), "en.phone lexicon is upper case")


if __name__ == "__main__":
    unittest.main()
