"""Plan B camera source: upstream's browser relay, with detections fed to follow. No motors.

Use only if reading the camera directly (mediad stopped) does not work. mediad keeps running
(its auto-exposure too); a browser on the operator's laptop shows the WebRTC video and uploads
JPEGs; this process answers them with upstream's own serve.py/ArucoDetector and ALSO prints
every result as a JSON line for follow/sources.py:

    {"kind": "frame", "captured": <monotonic>, "detections": {...}}

It imports upstream vision_follow/serve.py unchanged and only wraps the detector, so the page,
/config.json, the one-frame-at-a-time lock and the upload checks are upstream's. The served
page has its own control buttons hidden (起身使能 / 启动注视): follow drives the robot, not the page.

The browser does not send its capture time, so `captured` is the moment detection started minus
--latency-margin (default 0.5 s): frames are treated as older than they are, never newer, so a
frame taken before a stop can not pass for one taken after it.

    ~/microduck-vision/.venv/bin/python follow/vision/relay_worker.py --vision-dir ~/microduck-vision
    then open http://<duck-ip>:8091/ → 连接摄像头 → tick 仅预览时启用 ArUco 识别
"""

from __future__ import annotations

import argparse
import functools
import json
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

BANNER = ("<style>#enable,#start{display:none!important}</style>"
          "<div style='background:#7a4b00;color:#fff;padding:10px 24px;font:600 16px system-ui'>"
          "follow 中繼模式：這個頁面只負責把畫面送去辨識。控制由鴨子上的 follow 程式負責。"
          "請按「连接摄像头」，再勾選「仅预览时启用 ArUco 识别」，保持這個分頁在前景。</div>")
_print_lock = threading.Lock()


def emit(kind: str, **fields) -> None:
    with _print_lock:
        print(json.dumps({"kind": kind, "t": round(time.monotonic(), 4), **fields}, allow_nan=False), flush=True)


class TapEngine:
    """Wraps upstream ArucoDetector: same answers to the browser, plus a JSON line for follow."""

    def __init__(self, inner, latency_margin: float, clock=time.monotonic, out=emit):
        self.inner = inner
        self.latency_margin = latency_margin
        self._clock = clock
        self._emit = out

    def info(self):
        return self.inner.info()

    def detect(self, jpeg, rotate):
        started = self._clock()
        result = self.inner.detect(jpeg, rotate)
        self._emit("frame", captured=round(started - self.latency_margin, 4), detections=result)
        return result


def inject_banner(html: bytes) -> bytes:
    return html.replace(b"<main>", b"<main>" + BANNER.encode("utf-8"), 1)


def make_handler(serve_module, directory: str):
    class RelayHandler(serve_module.Handler):
        def do_POST(self):
            # Every upload becomes a live frame that steers the duck, so only the first client to
            # upload (the operator's laptop) is listened to; anyone else on the venue LAN is refused.
            client = self.client_address[0]
            with self.server.client_lock:
                if self.server.client_ip is None:
                    self.server.client_ip = client
                    emit("warning", message=f"中繼：只接受 {client} 上傳的畫面")
                allowed = client == self.server.client_ip
            if not allowed:
                self.json_response(403, {"error": f"只接受 {self.server.client_ip} 的畫面"})
                return
            super().do_POST()

        def send_demo(self, path, body):
            if path in ("/", "/index.html"):
                payload = inject_banner((Path(self.directory) / "index.html").read_bytes())
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(payload)
                return
            super().send_demo(path, body)

    return functools.partial(RelayHandler, directory=directory)


def build_server(vision_dir: Path, *, bind: str, port: int, marker_id: int, hz: float, marker_mm: float,
                 latency_margin: float, engine=None):
    sys.path.insert(0, str(vision_dir))
    import serve  # upstream vision_follow/serve.py
    args = serve.parse_args(["--detector", "aruco", "--bind", bind, "--port", str(port), "--marker-id", str(marker_id),
                             "--aruco-hz", str(hz), "--marker-mm", str(marker_mm)])
    if engine is None:
        from aruco_detector import ArucoDetector
        engine = ArucoDetector(args.marker_mm, args.calibration)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(serve, str(vision_dir)))
    # Same config upstream serve.main() builds, so the page behaves exactly as upstream.
    server.demo_config = {"robotHost": args.robot_host, "signallingPort": args.signalling_port,
                          "detector": "aruco", "markerId": args.marker_id, "arucoHz": args.aruco_hz,
                          "arucoMaxSide": args.aruco_max_side, "arucoJpegQuality": args.aruco_jpeg_quality,
                          "detectionMaxAge": args.detection_max_age, "arucoHttpTimeout": args.aruco_http_timeout,
                          "targetTimeout": args.target_timeout, "videoTimeout": args.video_timeout,
                          "followStyle": args.follow_style, "followStepSeconds": args.follow_step_seconds,
                          "followSettleSeconds": args.follow_settle_seconds,
                          "followRequireFront": args.follow_require_front, "aruco": engine.info()}
    server.aruco_engine = TapEngine(engine, latency_margin)
    server.aruco_lock = threading.Lock()
    server.aruco_upload_timeout = args.aruco_http_timeout + 1
    server.client_lock = threading.Lock()
    server.client_ip = None
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vision-dir", type=Path, default=Path.home() / "microduck-vision")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--marker-id", type=int, default=0)
    parser.add_argument("--marker-mm", type=float, default=50.0)
    parser.add_argument("--hz", type=float, default=2.0, help="browser upload ceiling (upstream default .5)")
    parser.add_argument("--latency-margin", type=float, default=0.5)
    args = parser.parse_args(argv)
    if not 0.0 <= args.latency_margin <= 3.0:
        parser.error("--latency-margin must be 0..3 s")
    server = build_server(args.vision_dir, bind=args.bind, port=args.port, marker_id=args.marker_id,
                          hz=args.hz, marker_mm=args.marker_mm, latency_margin=args.latency_margin)
    emit("ready", source=f"browser relay http://<鴨子IP>:{args.port}/", rotate="(由網頁依 media.video 決定)", hz=args.hz)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, ImportError, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code in (0, None):
            raise
        emit("error", message=str(error))
        sys.exit(1)
