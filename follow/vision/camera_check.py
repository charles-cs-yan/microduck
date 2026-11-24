"""Verify reading the head camera without mediad. Moves nothing; needs mediad already stopped.

Run on the duck with the vision environment's Python:

    ~/microduck-vision/.venv/bin/python follow/vision/camera_check.py --device /dev/videoN

Checks, in order, and writes everything to --out (default ~/microduck-follow/camera-check-<time>/):
  1. mediad is not active (refuses otherwise — it holds the camera; this script never stops it)
  2. v4l2 devices, formats, and the exposure/gain controls on --device
  3. gst-launch capture for --seconds: frames received, real rate, mean luma per frame
  4. ArUco detection on every frame (ids, size, x position after --rotate)
  5. first and last JPEG saved, to judge the rotation by eye
  6. auto-exposure (default on; --no-auto-exposure turns it off): the AE loop runs and the
     luma trajectory is reported (writes exposure controls to the sensor — camera only, no motors)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from aruco_worker import gst_command, load_detector, split_jpegs  # noqa: E402


def sh(*command: str) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"<{' '.join(command)} failed: {error}>"
    return (result.stdout + result.stderr).strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--rate", type=float, default=5.0, help="capture rate cap for the check")
    parser.add_argument("--rotate", type=int, default=90, choices=(0, 90, 180, 270))
    parser.add_argument("--vision-dir", type=Path, default=Path.home() / "microduck-vision")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--no-auto-exposure", dest="auto_exposure", action="store_false",
                        help="auto-exposure is on by default: without mediad the sensor stays dark")
    args = parser.parse_args(argv)

    if sh("systemctl", "is-active", "mediad") == "active":
        print("mediad 還在執行（它占用相機）。請先確認後由人執行 sudo systemctl stop mediad，再跑這個檢查。")
        return 2
    out = args.out or Path.home() / "microduck-follow" / f"camera-check-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    report = {"device": args.device, "rotate": args.rotate, "seconds": args.seconds}

    print("== v4l2 裝置")
    report["devices"] = sh("v4l2-ctl", "--list-devices")
    print(report["devices"])
    report["formats"] = sh("v4l2-ctl", "-d", args.device, "--list-formats-ext")
    report["controls"] = "\n".join(line for line in sh("v4l2-ctl", "-d", args.device, "--list-ctrls").splitlines()
                                   if any(k in line for k in ("exposure", "gain")))
    print("== 曝光／增益控制\n" + (report["controls"] or "（這個節點沒有 exposure/gain 控制）"))

    detector = load_detector(args.vision_dir, 50.0)
    loop = None
    if args.auto_exposure:
        from exposure import AeLoop, V4l2Writer
        loop = AeLoop(V4l2Writer(args.device))
    from exposure import mean_luma_from_jpeg

    command = gst_command(args.device, args.rate, 640, 360)
    report["gst"] = " ".join(command)
    print("== 擷取：" + report["gst"])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frames, first, last = [], None, None
    started = time.monotonic()
    try:
        for jpeg in split_jpegs(process.stdout):
            now = time.monotonic()
            mean = mean_luma_from_jpeg(jpeg)
            written = loop.feed(mean, now) if loop is not None and mean is not None else None
            try:
                det = detector.detect(jpeg, args.rotate)
                markers = [{"id": b["id"], "size": round(b["size_norm"], 3),
                            "x": round((b["x0"] + b["x1"]) / det["width"] - 1, 2)} for b in det["boxes"]]
            except ValueError as error:
                det, markers = None, [{"error": str(error)}]
            frames.append({"t": round(now - started, 2), "bytes": len(jpeg),
                           "luma": None if mean is None else round(mean, 1), "markers": markers,
                           "exposure": None if written is None else written.__dict__,
                           "size": None if det is None else [det["width"], det["height"]]})
            first = first or jpeg
            last = jpeg
            print(f"{frames[-1]['t']:5.2f}s luma={frames[-1]['luma']} markers={markers}"
                  + (f" AE→{written}" if written else ""))
            if now - started >= args.seconds:
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        stderr = process.stderr.read().decode(errors="replace").strip()
    if first:
        (out / "first.jpg").write_bytes(first)
        (out / "last.jpg").write_bytes(last)
    elapsed = (frames[-1]["t"] - frames[0]["t"]) if len(frames) > 1 else 0.0
    report.update(frames=frames, frame_count=len(frames),
                  rate=round((len(frames) - 1) / elapsed, 2) if elapsed else None,
                  gst_stderr=stderr[-2000:], marker_frames=sum(1 for f in frames if any("id" in m for m in f["markers"])))
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== 摘要：{len(frames)} 張，約 {report['rate']} fps（上限 {args.rate}），"
          f"{report['marker_frames']} 張看到 marker")
    if stderr:
        print("gst-launch 訊息：" + stderr[-500:])
    print(f"檔案在 {out}（first.jpg / last.jpg / report.json）")
    return 0 if frames else 1


if __name__ == "__main__":
    sys.exit(main())
