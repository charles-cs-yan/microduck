"""End-to-end vision check with real OpenCV: synthetic camera frames → aruco_worker → Tracker.

Skipped unless OpenCV with aruco is importable (run with the vision venv):

    ~/mdev/vision-venv/bin/python -m unittest follow.tests.test_integration_vision

The camera is mounted a quarter turn off (mediad), so a raw frame is the upright scene turned
90° anticlockwise and the worker's --rotate 90 (clockwise) must put the marker back where it was.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VISION_DIR = ROOT / "microduck" / "vision_follow"
WORKER = ROOT / "follow" / "vision" / "aruco_worker.py"

try:
    import cv2
    import numpy as np
    HAVE_CV = hasattr(cv2, "aruco")
except ImportError:  # pragma: no cover - plain environments
    HAVE_CV = False


def upright_scene(cx, cy, side, width=360, height=640, marker_id=0):
    """Portrait (upright) grey scene with one DICT_5X5_50 marker centred at (cx, cy) pixels."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_50)
    marker = cv2.aruco.generateImageMarker(dictionary, marker_id, side)
    scene = np.full((height, width), 200, np.uint8)
    pad = side // 5  # white quiet zone
    x0, y0 = int(cx - side / 2), int(cy - side / 2)
    scene[y0 - pad:y0 + side + pad, x0 - pad:x0 + side + pad] = 255
    scene[y0:y0 + side, x0:x0 + side] = marker
    return scene


def raw_jpeg(scene):
    raw = cv2.rotate(scene, cv2.ROTATE_90_COUNTERCLOCKWISE)  # what the quarter-turned sensor sees
    ok, data = cv2.imencode(".jpg", raw, [cv2.IMWRITE_JPEG_QUALITY, 80])
    assert ok
    return data.tobytes()


def run_worker(jpegs, rotate=90):
    result = subprocess.run(
        [sys.executable, str(WORKER), "--vision-dir", str(VISION_DIR), "--stdin", "--rotate", str(rotate), "--hz", "5"],
        input=b"".join(jpegs), capture_output=True, timeout=60)
    lines = [json.loads(l) for l in result.stdout.decode().splitlines() if l.strip()]
    return lines, result


@unittest.skipUnless(HAVE_CV and VISION_DIR.is_dir(), "needs OpenCV aruco (vision venv) and the upstream checkout")
class VisionIntegrationTest(unittest.TestCase):
    def test_rotation_puts_marker_back_upright(self):
        lines, result = run_worker([raw_jpeg(upright_scene(cx=90, cy=320, side=100))])
        self.assertEqual(result.returncode, 0, result.stderr.decode()[-500:])
        frames = [l for l in lines if l["kind"] == "frame"]
        self.assertEqual(len(frames), 1, lines)
        det = frames[0]["detections"]
        self.assertEqual((det["width"], det["height"]), (360, 640), "upright portrait after --rotate 90")
        (box,) = det["boxes"]
        self.assertEqual(box["id"], 0)
        self.assertAlmostEqual((box["x0"] + box["x1"]) / 2, 90, delta=4, msg="marker stays on the left")
        self.assertAlmostEqual((box["y0"] + box["y1"]) / 2, 320, delta=4)

    def test_wrong_rotation_is_detectable(self):
        lines, _ = run_worker([raw_jpeg(upright_scene(cx=90, cy=320, side=100))], rotate=0)
        det = next(l for l in lines if l["kind"] == "frame")["detections"]
        self.assertEqual((det["width"], det["height"]), (640, 360), "rotate 0 leaves it landscape")

    def test_worker_output_drives_tracker(self):
        from follow.tracker import Tracker, boxes_from
        far = raw_jpeg(upright_scene(cx=180, cy=320, side=60))     # small → far → forward
        near = raw_jpeg(upright_scene(cx=180, cy=320, side=300))   # big → near → backward
        for jpeg, expected in ((far, 1), (near, -1)):
            lines, _ = run_worker([jpeg])
            det = next(l for l in lines if l["kind"] == "frame")["detections"]
            self.assertEqual(len(boxes_from(det)), 1)
            t = Tracker(target_id=0)
            t.ingest(det, 0.0)
            t.ingest(det, 0.5)
            vx = t.output(0.5, "follow", 0.1).desired_vx
            self.assertEqual((vx > 0) - (vx < 0), expected, (expected, boxes_from(det)[0].size))

    def test_no_marker_and_other_ids(self):
        from follow.tracker import Tracker
        blank = cv2.imencode(".jpg", np.full((640, 360), 200, np.uint8))[1].tobytes()
        # One worker run per frame: the worker paces detection, so frames sent together are dropped.
        lines, _ = run_worker([blank])
        self.assertEqual(next(l for l in lines if l["kind"] == "frame")["detections"]["boxes"], [])
        lines, _ = run_worker([raw_jpeg(upright_scene(180, 320, 100, marker_id=7))])
        det = next(l for l in lines if l["kind"] == "frame")["detections"]
        self.assertEqual([b["id"] for b in det["boxes"]], [7], "the other marker is detected…")
        t = Tracker(target_id=0)
        t.ingest(det, 0.0)
        self.assertIsNone(t.target, "…but marker 7 is not the host's marker")

    def test_pacing_drops_back_to_back_frames(self):
        jpeg = raw_jpeg(upright_scene(180, 320, 100))
        lines, _ = run_worker([jpeg] * 10)
        self.assertEqual(sum(l["kind"] == "frame" for l in lines), 1, "10 frames at once → 1 detection at ≤5 Hz")

    def test_browser_relay_end_to_end(self):
        """Plan B: upstream serve.py unchanged, detections tapped to stdout for follow."""
        import socket
        import time
        import urllib.request
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        relay = WORKER.parent / "relay_worker.py"
        process = subprocess.Popen([sys.executable, "-u", str(relay), "--vision-dir", str(VISION_DIR),
                                    "--bind", "127.0.0.1", "--port", str(port)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            ready = json.loads(process.stdout.readline())
            self.assertEqual(ready["kind"], "ready", ready)
            base = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    page = urllib.request.urlopen(base + "/", timeout=2).read().decode()
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertIn("follow 中繼模式", page)
            self.assertIn("#enable,#start{display:none", page, "the page's own control buttons are hidden")
            config = json.loads(urllib.request.urlopen(base + "/config.json", timeout=2).read())
            self.assertEqual((config["detector"], config["markerId"], config["arucoHz"]), ("aruco", 0, 2.0))
            jpeg = raw_jpeg(upright_scene(cx=90, cy=320, side=100))
            request = urllib.request.Request(base + "/aruco/detect?rotate=90&frame_id=7", data=jpeg,
                                             headers={"Content-Type": "image/jpeg"}, method="POST")
            before = time.monotonic()
            answer = json.loads(urllib.request.urlopen(request, timeout=10).read())
            self.assertEqual(answer["frame_id"], 7, "the browser gets upstream's normal reply")
            self.assertEqual([b["id"] for b in answer["boxes"]], [0])
            line = json.loads(process.stdout.readline())
            self.assertEqual(line["kind"], "warning", "first uploader is announced (client lock)")
            self.assertIn("127.0.0.1", line["message"])
            line = json.loads(process.stdout.readline())
            self.assertEqual(line["kind"], "frame")
            self.assertEqual(line["detections"]["boxes"][0]["id"], 0)
            self.assertAlmostEqual((line["detections"]["boxes"][0]["x0"] + line["detections"]["boxes"][0]["x1"]) / 2, 90, delta=4)
            self.assertLess(line["captured"], before, "captured is pushed back by the latency margin")
            self.assertGreater(line["captured"], before - 1.0)
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_exposure_metering(self):
        sys.path.insert(0, str(WORKER.parent))
        from exposure import mean_luma_from_jpeg
        dark = cv2.imencode(".jpg", np.full((360, 640), 40, np.uint8))[1].tobytes()
        bright = cv2.imencode(".jpg", np.full((360, 640), 220, np.uint8))[1].tobytes()
        self.assertAlmostEqual(mean_luma_from_jpeg(dark), 40, delta=2)
        self.assertAlmostEqual(mean_luma_from_jpeg(bright), 220, delta=2)


if __name__ == "__main__":
    unittest.main()
