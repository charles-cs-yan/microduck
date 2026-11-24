import io
import unittest
from types import SimpleNamespace

from follow.sources import Frame
from follow.status import CLEAR, StatusPrinter, compose


class Tty(io.StringIO):
    def isatty(self):
        return True


def fake_app(mode="follow", problem="", reason="短程前進", phase="moving", volts=8.1, cpu=61.0,
             battery_low="", dry_run=False):
    guard = SimpleNamespace(health={"battery": {"volts": volts}, "cpu_temp_c": cpu},
                            battery_low=battery_low, dry_run=dry_run)
    return SimpleNamespace(mode=mode, problem=problem, guard=guard,
                           last_decision=SimpleNamespace(reason=reason, phase=phase))


class ComposeTest(unittest.TestCase):
    def test_follow_line(self):
        camera = SimpleNamespace(latest=lambda: Frame(9.6, 9.6, {}), error="")
        voice = SimpleNamespace(status=lambda: "語音正常")
        line = compose(fake_app(), 10.0, voice=voice, camera=camera, heard="follow")
        self.assertIn("[follow·moving] 短程前進", line)
        self.assertIn("8.10V 61°C", line)
        self.assertIn("相機 0.4s", line)
        self.assertIn("語音正常 · 上一句 follow", line)
        self.assertNotIn("DRY", line)

    def test_problem_battery_dry_and_idle(self):
        line = compose(fake_app(problem="IMU 未就緒", battery_low="7.55 V", dry_run=True), 0)
        self.assertIn("不可移動：IMU 未就緒", line)
        self.assertIn("⚠請充電", line)
        self.assertIn("DRY", line)
        self.assertIn("待命", compose(fake_app(mode="idle"), 0))
        app = fake_app()
        app.guard.health = None
        self.assertIn("?V", compose(app, 0))

    def test_camera_states(self):
        waiting = SimpleNamespace(latest=lambda: None, error="")
        broken = SimpleNamespace(latest=lambda: None, error="device busy")
        self.assertIn("相機等待中", compose(fake_app(), 0, camera=waiting))
        self.assertIn("相機✗", compose(fake_app(), 0, camera=broken))


class PrinterTest(unittest.TestCase):
    def test_tty_repaints_in_place_and_logs_above(self):
        now = [0.0]
        out = Tty()
        printer = StatusPrinter(out, clock=lambda: now[0])
        printer.update("A")
        printer.update("A")  # unchanged and not due: nothing
        printer.log("event")
        now[0] += 1
        printer.update("A")  # due: repaint
        printer.finish()
        text = out.getvalue()
        self.assertEqual(text, CLEAR + "A" + CLEAR + "event\nA" + CLEAR + "A" + "\n")

    def test_pipe_prints_lines_at_pipe_period(self):
        now = [0.0]
        out = io.StringIO()
        printer = StatusPrinter(out, pipe_period=5.0, clock=lambda: now[0])
        for i in range(12):
            printer.update(f"s{i}")
            now[0] += 1
        printer.log("event")
        self.assertEqual(out.getvalue().splitlines(), ["s0", "s5", "s10", "event"])
        self.assertNotIn("\033", out.getvalue())

    def test_fit_counts_wide_characters(self):
        self.assertEqual(StatusPrinter._fit("跟隨跟隨跟隨", columns=7), "跟隨跟")
        self.assertEqual(StatusPrinter._fit("abcdef", columns=4), "abc")


if __name__ == "__main__":
    unittest.main()
