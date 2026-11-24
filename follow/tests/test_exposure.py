"""Auto-exposure port: same behaviour as mediad/src/exposure.rs (rev 04b1c00)."""

import unittest
from types import SimpleNamespace

from follow.vision.exposure import (HARD_LINES, MAX_ANALOGUE, MAX_DIGITAL, REASSERT_TICKS, SOFT_LINES,
                                    TARGET_Y, Ae, AeLoop, Controls, V4l2Writer)


def luma_for(ae, light):
    """Toy sensor: luma ∝ light × exposure × gains, gamma-compressed and clipped like the ISP."""
    linear = light * ae.exposure * ae.analogue * ae.digital
    return min(255.0, 255.0 * (linear / 1e6) ** 0.45)


class AeTest(unittest.TestCase):
    def test_deadband(self):
        ae = Ae(600, 1024)
        self.assertIsNone(ae.step(TARGET_Y))
        self.assertIsNone(ae.step(TARGET_Y * 1.1))
        self.assertIsNotNone(ae.step(TARGET_Y * 1.5))

    def test_noise_order_brightening(self):
        ae = Ae(100, 256)
        ae.step(10)  # far too dark: ratio 9 is clamped to 4, damped to 4**0.6 ≈ 2.30
        c = ae.controls()
        self.assertEqual(c.exposure, int(100 * 4 ** 0.6))
        self.assertEqual(c.analogue_gain, 256, "shutter is spent before gain")
        for _ in range(40):
            ae.step(5)
        c = ae.controls()
        self.assertEqual(c.exposure, int(HARD_LINES))
        self.assertEqual(c.analogue_gain, int(MAX_ANALOGUE * 256))
        self.assertEqual(c.gain, int(MAX_DIGITAL * 256))

    def test_shutter_first_then_gain(self):
        ae = Ae(100, 256)
        for _ in range(3):
            ae.step(30)
        self.assertLessEqual(ae.exposure, SOFT_LINES)
        if ae.exposure < SOFT_LINES:
            self.assertEqual(ae.analogue, 1.0, "gain only once the soft shutter cap is reached")

    def test_never_exceeds_hard_cap(self):
        ae = Ae(5000, 99999)
        self.assertLessEqual(ae.exposure, HARD_LINES)
        self.assertLessEqual(ae.analogue, MAX_ANALOGUE)

    def test_converges_from_bright_and_dark(self):
        for light in (1.0, 5.0, 60.0):  # all reachable: at 1.0 the ceilings give luma ≈ 127
            ae = Ae(600, 1024)
            for _ in range(60):
                ae.step(luma_for(ae, light))
            self.assertLess(abs(luma_for(ae, light) / TARGET_Y - 1), 0.2, light)

    def test_unreachable_room_stops_writing(self):
        ae = Ae(600, 1024)
        writes = [ae.step(luma_for(ae, 0.2)) for _ in range(60)]  # ceilings give luma ≈ 62 < 90
        self.assertIsNone(writes[-1], "at every ceiling the same values are not written again")
        self.assertEqual(ae.controls(), Controls(int(HARD_LINES), int(MAX_ANALOGUE * 256), int(MAX_DIGITAL * 256)))


class FakeWriter:
    def __init__(self, fail=False):
        self.writes, self.fail = [], fail

    def read(self, name):
        return {"exposure": 600, "analogue_gain": 1024}[name]

    def write(self, c, force_gain=False):
        if self.fail:
            raise OSError("no such control")
        self.writes.append(c)
        self.forced = getattr(self, "forced", []) + [force_gain]


class AeLoopTest(unittest.TestCase):
    def test_paced_and_reasserts(self):
        writer = FakeWriter()
        loop = AeLoop(writer)
        self.assertIsNotNone(loop.feed(20, 0.0))
        self.assertIsNone(loop.feed(20, 0.2), "at most every 0.5 s")
        writes = len(writer.writes)
        t = 1.0
        for _ in range(REASSERT_TICKS + 1):
            loop.feed(TARGET_Y, t)
            t += 0.5
        self.assertEqual(len(writer.writes), writes + 1, "one re-assert after 20 quiet ticks")

    def test_first_write_and_reasserts_force_the_isp_gain(self):
        """Review #12: mediad may leave a high digital gain; the first write puts it back to 1x."""
        writer = FakeWriter()
        loop = AeLoop(writer)
        loop.feed(TARGET_Y, 0.0)  # inside the deadband, but nothing synced yet → one write
        self.assertEqual(len(writer.writes), 1)
        self.assertEqual(writer.forced, [True])
        t = 1.0
        for _ in range(REASSERT_TICKS + 1):
            loop.feed(TARGET_Y, t)
            t += 0.5
        self.assertEqual(writer.forced[-1], True, "re-asserts include the gain")
        loop.feed(20, t + 1)
        self.assertEqual(writer.forced[-1], False, "ordinary steps only write gain when > 1x")

    def test_write_failure_reported_once(self):
        logs = []
        loop = AeLoop(FakeWriter(fail=True), log=logs.append)
        loop.feed(10, 0.0)
        loop.feed(10, 1.0)
        self.assertEqual(len(logs), 1)


class V4l2WriterTest(unittest.TestCase):
    def test_two_calls_and_skip_unity_gain(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="exposure: 600\n", stderr="")

        writer = V4l2Writer("/dev/video9", run=run)
        writer.write(Controls(600, 1024, 256))
        self.assertEqual(calls, [["v4l2-ctl", "-d", "/dev/video9", "--set-ctrl=exposure=600,analogue_gain=1024"]])
        writer.write(Controls(1200, 2816, 512))
        self.assertEqual(calls[-1], ["v4l2-ctl", "-d", "/dev/video9", "--set-ctrl=gain=512"])
        self.assertEqual(writer.read("exposure"), 600)

    def test_forced_unity_gain_and_missing_gain_control(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command[-1])
            failing = command[-1].startswith("--set-ctrl=gain")
            return SimpleNamespace(returncode=1 if failing else 0, stdout="", stderr="unknown control 'gain'")

        writer = V4l2Writer("/dev/video9", run=run)
        writer.write(Controls(600, 1024, 256), force_gain=True)  # node without gain: tolerated at 1x
        self.assertEqual(calls, ["--set-ctrl=exposure=600,analogue_gain=1024", "--set-ctrl=gain=256"])
        self.assertTrue(writer.gain_unsupported)
        writer.write(Controls(600, 1024, 256), force_gain=True)
        self.assertEqual(len(calls), 3, "not retried once known missing")
        with self.assertRaises(OSError):
            writer.write(Controls(1200, 2816, 512))  # but > 1x really needs it

    def test_timeout_becomes_oserror(self):
        import subprocess

        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

        writer = V4l2Writer("/dev/video9", run=run)
        with self.assertRaises(OSError):
            writer.write(Controls(600, 1024, 256))
        self.assertIsNone(writer.read("exposure"))

    def test_failure_raises(self):
        writer = V4l2Writer("/dev/video9", run=lambda c, **k: SimpleNamespace(returncode=1, stdout="", stderr="bad"))
        with self.assertRaises(OSError):
            writer.write(Controls(600, 1024, 256))
        self.assertIsNone(writer.read("exposure"))


if __name__ == "__main__":
    unittest.main()
