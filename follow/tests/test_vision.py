import io
import unittest.mock
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from follow import app as app_module
from follow.sources import ProcessDetectionSource
from follow.tests.test_robotd_client import wait_for
from follow.vision import aruco_worker
from follow.vision.aruco_worker import gst_command, split_jpegs

JPEG_A = b"\xff\xd8" + b"A" * 50 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"\x00\xff\x00B" * 30 + b"\xff\xd9"


class Trickle(io.RawIOBase):
    """A stream that hands out a few bytes per read, like a pipe under load."""

    def __init__(self, data, size=7):
        self.data, self.size = data, size

    def readable(self):
        return True

    def read(self, n=-1):
        chunk, self.data = self.data[:self.size], self.data[self.size:]
        return chunk


class SplitTest(unittest.TestCase):
    def test_back_to_back(self):
        self.assertEqual(list(split_jpegs(io.BytesIO(JPEG_A + JPEG_B + JPEG_A))), [JPEG_A, JPEG_B, JPEG_A])

    def test_split_across_reads(self):
        self.assertEqual(list(split_jpegs(Trickle(JPEG_A + JPEG_B), 7)), [JPEG_A, JPEG_B])
        self.assertEqual(list(split_jpegs(Trickle(JPEG_A + JPEG_B, 1), 1)), [JPEG_A, JPEG_B])

    def test_garbage_and_truncation(self):
        stream = io.BytesIO(b"junk" + JPEG_A + b"more junk" + JPEG_B + b"\xff\xd8 never ends")
        self.assertEqual(list(split_jpegs(stream)), [JPEG_A, JPEG_B])

    def test_gst_command_is_paced_and_scaled(self):
        command = " ".join(gst_command("/dev/video0", 2.0, 640, 360))
        self.assertIn("device=/dev/video0", command)
        self.assertIn("max-rate=2", command)
        self.assertIn("width=640,height=360", command)


class StubDetector:
    def __init__(self):
        self.calls = []

    def detect(self, jpeg, rotate):
        self.calls.append((len(jpeg), rotate))
        if jpeg == JPEG_B:
            raise ValueError("bad jpeg")
        return {"width": 360, "height": 640, "boxes": []}


class WorkerRunTest(unittest.TestCase):
    def run_worker(self, frames, hz=1e9, detector=None, clock=None):  # effectively unpaced unless asked
        detector, out = detector or StubDetector(), io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            if clock is None:
                aruco_worker.run(iter(frames), detector, 90, hz)
            else:
                aruco_worker.run(iter(frames), detector, 90, hz, clock=clock)
        finally:
            sys.stdout = real
        return detector, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_emits_frames_and_bad_frame_is_only_a_warning(self):
        times = iter([0.0, 0.0, 0.6, 0.6])  # read, after-processing for each of the two frames
        detector, lines = self.run_worker([JPEG_A, JPEG_B], hz=2.0, clock=lambda: next(times))
        kinds = [l["kind"] for l in lines]
        self.assertEqual(kinds, ["frame", "warning"], "one bad frame must not mark the camera broken")
        self.assertEqual(lines[0]["detections"]["height"], 640)
        self.assertLessEqual(lines[0]["captured"], time.monotonic())
        self.assertEqual(detector.calls[0][1], 90)

    def test_pacing_drops_frames(self):
        detector, lines = self.run_worker([JPEG_A] * 50, hz=0.5)
        self.assertEqual(len(detector.calls), 1, "50 frames arriving at once → one detection at 0.5 Hz")

    def test_pacing_tolerates_jitter(self):
        """Review #5: gst delivers ~every 0.5 s with jitter; a frame 20 ms early must still count."""
        times = iter(t for base in (0.0, 0.48, 0.97, 1.44, 1.95) for t in (base, base + 0.01))
        detector, lines = self.run_worker([JPEG_A] * 5, hz=2.0, clock=lambda: next(times))
        self.assertEqual(len(detector.calls), 5)

    def test_stall_drops_queued_frames(self):
        """Review #7: frames read right after a long stall have misleadingly fresh times."""
        now = [0.0]

        class Slow(StubDetector):
            def detect(self, jpeg, rotate):
                result = super().detect(jpeg, rotate)
                if len(self.calls) == 1:
                    now[0] += 2.0  # e.g. a hung v4l2-ctl
                return result

        def clock():
            return now[0]

        def frames():
            for _ in range(6):
                yield JPEG_A
                now[0] += 0.01  # queued frames are read back to back
            now[0] += 1.0
            yield JPEG_A  # a genuinely new frame after the distrust window

        detector, lines = self.run_worker(frames(), hz=2.0, detector=Slow(), clock=clock)
        self.assertEqual(len(detector.calls), 2, "only the first frame and the fresh one")
        self.assertTrue(any("停頓" in l.get("message", "") for l in lines))


VISION_DIR = Path(__file__).resolve().parents[2] / "microduck" / "vision_follow"


@unittest.skipUnless((VISION_DIR / "serve.py").is_file(), "needs the upstream checkout")
class RelayClientLockTest(unittest.TestCase):
    """Review #13: only the first uploader steers the duck."""

    def test_second_client_is_refused(self):
        import threading
        import urllib.error
        import urllib.request
        from follow.vision import relay_worker

        class Engine:
            def info(self):
                return {"dictionary": "DICT_5X5_50"}

            def detect(self, jpeg, rotate):
                return {"source": "aruco", "width": 360, "height": 640, "boxes": []}

        import socket
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        # Upstream serve.py refuses port 0, so probe for a free port and bind it. Probing then
        # binding is racy (WSL2 forwards localhost ports into Windows): retry a few times.
        server = None
        for _ in range(5):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            try:
                server = relay_worker.build_server(VISION_DIR, bind="127.0.0.1", port=port, marker_id=0, hz=2.0,
                                                   marker_mm=50.0, latency_margin=0.5, engine=Engine())
                break
            except OSError:
                continue
        if server is None:
            sys.stdout = real
            self.fail("could not bind a free port in 5 tries")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/aruco/detect?rotate=90&frame_id=1"

            def post():
                request = urllib.request.Request(url, data=JPEG_A, headers={"Content-Type": "image/jpeg"}, method="POST")
                return urllib.request.urlopen(request, timeout=5)

            self.assertEqual(json.loads(post().read())["frame_id"], 1)
            self.assertEqual(server.client_ip, "127.0.0.1")
            server.client_ip = "10.9.9.9"  # as if another machine had uploaded first
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post()
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            sys.stdout = real
            sys.path[:] = [p for p in sys.path if p != str(VISION_DIR)]
        frames = [json.loads(l) for l in out.getvalue().splitlines() if '"frame"' in l]
        self.assertEqual(len(frames), 1, "the refused upload never became a frame")


class ProcessSourceTest(unittest.TestCase):
    def test_parsing(self):
        now = [100.0]
        logs = []
        source = ProcessDetectionSource(live=True, clock=lambda: now[0], log=logs.append)
        self.assertIsNone(source.latest())
        source.handle_line(json.dumps({"kind": "ready", "source": "/dev/video0", "rotate": 90, "hz": 2}))
        self.assertTrue(source.ready)
        source.handle_line(json.dumps({"kind": "frame", "captured": 99.5, "detections": {"boxes": []}}))
        frame = source.latest()
        self.assertEqual((frame.captured_at, frame.video_at), (99.5, 100.0))
        for bad in ("nope", json.dumps({"kind": "frame", "captured": "x", "detections": {}}),
                    json.dumps({"kind": "frame", "captured": 200.0, "detections": {}}),
                    json.dumps({"kind": "frame", "captured": True, "detections": {}})):
            source.handle_line(bad)
        self.assertEqual(source.latest().captured_at, 99.5, "bad and future-dated frames are ignored")
        source.handle_line(json.dumps({"kind": "error", "message": "device busy"}))
        self.assertIn("device busy", source.error)

    def test_child_process(self):
        script = textwrap.dedent("""
            import json, time
            print(json.dumps({"kind": "ready", "source": "fake", "rotate": 90, "hz": 2}), flush=True)
            print(json.dumps({"kind": "frame", "captured": time.monotonic(), "detections": {"boxes": []}}), flush=True)
            time.sleep(0.2)
        """)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.py"
            path.write_text(script, encoding="utf-8")
            source = ProcessDetectionSource(live=False, log=lambda s: None).start(sys.executable, str(path), [])
            try:
                self.assertTrue(wait_for(lambda: source.latest() is not None, timeout=5))
                self.assertTrue(wait_for(lambda: "結束" in source.error, timeout=5))
            finally:
                source.close()
        self.assertFalse(source.live)


class MainArgumentsTest(unittest.TestCase):
    def test_live_needs_camera(self):
        self.assertEqual(app_module.main(["--live"]), 2)
        self.assertEqual(app_module.main(["--live", "--replay", "x.jsonl"]), 2)

    def test_replay_and_camera_exclusive(self):
        self.assertEqual(app_module.main(["--replay", "x.jsonl", "--camera", "/dev/video0"]), 2)
        self.assertEqual(app_module.main(["--relay", "--camera", "/dev/video0"]), 2)
        self.assertEqual(app_module.main(["--relay", "9000", "--replay", "x.jsonl"]), 2)

    def test_live_prompt_refusal_starts_nothing(self):
        import subprocess as sp
        started = []
        real_popen = sp.Popen
        sp.Popen = lambda *a, **k: started.append(a) or real_popen(*a, **k)
        real_stdin = sys.stdin
        sys.stdin = io.StringIO("no\n")
        try:
            with unittest.mock.patch("builtins.print"):
                self.assertEqual(app_module.main(["--camera", "/dev/video0", "--live"]), 1)
        finally:
            sys.stdin = real_stdin
            sp.Popen = real_popen
        self.assertEqual(started, [], "review #6: no camera process before the operator says yes")


if __name__ == "__main__":
    unittest.main()
