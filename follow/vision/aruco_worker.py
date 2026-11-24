"""Camera → ArUco detections as JSON lines on stdout. No robot socket, no motors.

Runs in the vision environment (OpenCV with aruco, numpy), started by follow/sources.py:

    {"kind": "ready", ...}
    {"kind": "frame", "captured": <time.monotonic() when the JPEG arrived>, "detections": {...}}
    {"kind": "error", "message": "..."}

`detections` is upstream vision_follow/aruco_detector.py `ArucoDetector.detect()` output, the
format follow/tracker.py consumes. time.monotonic() is CLOCK_MONOTONIC, shared by every
process on the board, so `captured` is directly comparable with the app's clock.

Frame sources
  --device /dev/videoN   gst-launch reads the rkisp capture node directly. mediad holds the
                         camera while it runs, so this only works with mediad stopped.
                         UNVERIFIED on the board: caps and rate come from mediad's own notes
                         (UYVY 1280x720 off rkisp); check them before relying on this.
  --stdin                concatenated JPEGs on stdin (tests, or any other producer).

Detection is paced to --hz (default 2): full-rate detection reaches 95 °C and throttles the CPU.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, Iterator

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_JPEG = 4 * 1024 * 1024


def emit(kind: str, **fields) -> None:
    print(json.dumps({"kind": kind, "t": round(time.monotonic(), 4), **fields}, allow_nan=False), flush=True)


def split_jpegs(stream: BinaryIO, chunk: int = 65536) -> Iterator[bytes]:
    """Yield complete JPEGs from a byte stream of back-to-back JPEGs (jpegenc ! fdsink)."""
    buffer = b""
    read = getattr(stream, "read1", stream.read)  # read1: return what is there, do not wait to fill
    while True:
        data = read(chunk)
        if not data:
            return
        buffer += data
        while True:
            start = buffer.find(SOI)
            if start < 0:
                buffer = buffer[-1:]  # keep a possible half marker
                break
            end = buffer.find(EOI, start + 2)
            if end < 0:
                buffer = buffer[start:]
                if len(buffer) > MAX_JPEG:
                    buffer = b""  # garbage, resynchronise on the next SOI
                break
            yield buffer[start:end + 2]
            buffer = buffer[end + 2:]


def gst_command(device: str, hz: float, width: int, height: int) -> list:
    return ["gst-launch-1.0", "-q", "v4l2src", f"device={device}", "!",
            "video/x-raw,format=UYVY,width=1280,height=720", "!",
            "videorate", "drop-only=true", f"max-rate={max(1, int(round(hz)))}", "!",
            "videoconvert", "!", "videoscale", "!", f"video/x-raw,width={width},height={height}", "!",
            "jpegenc", "quality=80", "!", "fdsink", "fd=1"]


def load_detector(vision_dir: Path, marker_mm: float, calibration: str = None):
    sys.path.insert(0, str(vision_dir))
    from aruco_detector import ArucoDetector  # upstream vision_follow module
    return ArucoDetector(marker_mm=marker_mm, calibration=calibration)


def run(frames: Iterator[bytes], detector, rotate: int, hz: float, meter=None, clock=time.monotonic) -> int:
    """Detect at most `hz` times a second. meter(jpeg, now): optional per-frame hook (auto-exposure).

    * Pacing tolerates jitter: gst already caps its output at `hz`, so a frame that arrives up to
      a quarter period early is still used (a strict check halved the real rate).
    * `captured` is the time a frame is READ. If our own processing stalls longer than a period
      (a slow v4l2-ctl, a slow decode), frames queue in the pipe meanwhile and would be read later
      with a misleadingly fresh time — possibly passing for post-stop frames. So after such a
      stall everything read in the next 1.5 periods is dropped.
    * A single bad frame is a "warning", not an "error": the source stays healthy.
    """
    period = 1.0 / hz
    next_at = -float("inf")
    distrust_until = -float("inf")
    for jpeg in frames:
        captured = clock()
        if captured < distrust_until:
            continue
        if meter is not None:
            meter(jpeg, captured)
        if captured >= next_at - 0.25 * period:
            next_at = captured + period
            try:
                detections = detector.detect(jpeg, rotate)
            except ValueError as error:
                emit("warning", message=f"這張畫面偵測失敗：{error}")
            else:
                emit("frame", captured=round(captured, 4), detections=detections)
        busy = clock() - captured
        if busy > period:
            distrust_until = clock() + 1.5 * period
            emit("warning", message=f"處理停頓 {busy:.1f} 秒；接下來 {1.5 * period:.1f} 秒讀到的畫面可能是舊的，丟掉")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vision-dir", type=Path, default=Path.home() / "microduck-vision",
                        help="directory holding upstream aruco_detector.py")
    parser.add_argument("--device", help="rkisp video node for gst-launch (mediad must be stopped)")
    parser.add_argument("--stdin", action="store_true", help="read concatenated JPEGs from stdin")
    parser.add_argument("--rotate", type=int, default=90, choices=(0, 90, 180, 270),
                        help="clockwise degrees to upright the image (mediad: the mount is a quarter turn)")
    parser.add_argument("--hz", type=float, default=2.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--marker-mm", type=float, default=50.0)
    parser.add_argument("--calibration")
    parser.add_argument("--auto-exposure", action="store_true",
                        help="run mediad's software AE loop here (writes exposure via v4l2-ctl on --device)")
    args = parser.parse_args(argv)
    if bool(args.device) == bool(args.stdin):
        parser.error("choose exactly one of --device or --stdin")
    if args.auto_exposure and not args.device:
        parser.error("--auto-exposure needs --device")
    if not 0.2 <= args.hz <= 5:
        parser.error("--hz must be 0.2..5 (thermal limit)")

    detector = load_detector(args.vision_dir, args.marker_mm, args.calibration)
    emit("ready", source="stdin" if args.stdin else args.device, rotate=args.rotate, hz=args.hz)
    if args.stdin:
        return run(split_jpegs(sys.stdin.buffer), detector, args.rotate, args.hz)
    meter = None
    if args.auto_exposure:
        from exposure import AeLoop, V4l2Writer, mean_luma_from_jpeg  # same directory
        loop = AeLoop(V4l2Writer(args.device), log=lambda m: emit("warning", message=m))
        emit("exposure", start=True, exposure=loop.ae.controls().exposure,
             analogue_gain=loop.ae.controls().analogue_gain)

        def meter(jpeg, now):
            if now - loop.last_at < 0.5:
                return
            mean = mean_luma_from_jpeg(jpeg)
            if mean is not None:
                written = loop.feed(mean, now)
                if written is not None:
                    emit("exposure", mean_luma=round(mean, 1), exposure=written.exposure,
                         analogue_gain=written.analogue_gain, gain=written.gain)

    import tempfile
    # stderr to a file, not a pipe nobody reads: a chatty gst would fill a 64 KiB pipe and block.
    with tempfile.TemporaryFile() as gst_log:
        process = subprocess.Popen(gst_command(args.device, args.hz, args.width, args.height),
                                   stdout=subprocess.PIPE, stderr=gst_log)
        try:
            run(split_jpegs(process.stdout), detector, args.rotate, args.hz, meter)
            # stdout ended: gst is exiting. Wait for it before reading the code (poll() can race).
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = None
            gst_log.seek(0)
            message = gst_log.read().decode(errors="replace").strip()[-400:]
            emit("error", message=f"gst-launch 結束（{code}）：{message or '沒有訊息'}")
            return 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        emit("error", message=str(error))
        sys.exit(1)
