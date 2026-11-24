"""Microphone check on the duck: record after Enter, report levels and what the spotter hears.

    ~/microduck-follow/venv-voice/bin/python ~/microduck-follow/current/follow/voice/mic_check.py [--seconds 5]

No sudo, no motors, mediad may keep running (it does not hold the mic). Prints per-channel
peak / noise floor / loudest 0.3 s (≈ SNR) and the keywords found at several gains, and keeps
the WAV for later. Say a command (e.g. "follow me") once during the recording.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main(argv=None) -> int:
    import numpy as np
    from audio import dbfs, read_wav
    from models import FOLLOW_HOME

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="hw:microducki2s3,0")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--gains", default="5,10,20,30")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--no-prompt", action="store_true")
    args = parser.parse_args(argv)

    out = FOLLOW_HOME / f"mic-check-{time.strftime('%H%M%S')}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_prompt:
        input("準備好後按 Enter（倒數 3 秒後錄音，錄音時說一次指令，例如 follow me）")
        for i in (3, 2, 1):
            print(f"  {i}", flush=True)
            time.sleep(1)
    print(f"  >>> 錄音 {args.seconds} 秒：現在說 <<<", flush=True)
    subprocess.run(["arecord", "-q", "-D", args.device, "-f", "S32_LE", "-c", "2", "-r", "16000",
                    "-d", str(args.seconds), str(out)], check=True)

    print(f"== 存檔 {out}")
    worst = None
    for channel in (0, 1):
        x, rate = read_wav(str(out), channel)
        x = x[int(rate * 0.6):]  # skip the start-up transient (decays over ~0.5 s)
        blocks = x[: len(x) // 1600 * 1600].reshape(-1, 1600)
        rms = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1)) if len(blocks) else np.zeros(1)
        floor = float(np.percentile(rms, 20)) if rms.size else 0.0
        loud = float(np.max(np.convolve(rms, np.ones(3) / 3, mode="valid"))) if rms.size >= 3 else float(rms.max())
        snr = 20 * np.log10(max(loud, 1e-9) / max(floor, 1e-9))
        print(f"  ch{channel}: peak {20 * np.log10(max(float(np.abs(x).max()), 1e-9)):6.1f} dBFS  "
              f"整體 {dbfs(x):6.1f}  背景 {20 * np.log10(max(floor, 1e-9)):6.1f}  最大 0.3 s {20 * np.log10(max(loud, 1e-9)):6.1f}  "
              f"≈ 訊噪比 {snr:4.1f} dB")
        if channel == 0:
            worst = snr
    print("  （「最大 0.3 s」也包含拍手等聲響；只說話時才是語音的訊噪比）")
    if worst is not None and worst < 6:
        print("  ⚠ 左聲道訊噪比 < 6 dB：語音幾乎埋在噪音裡，請靠近一點或大聲一點")

    python = sys.executable
    for gain in args.gains.split(","):
        command = [python, str(HERE / "kws_listen.py"), "--wav", str(out), "--gain", gain]
        if args.model_dir:
            command += ["--model-dir", str(args.model_dir)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        import json
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        labels = [m.get("label") for m in messages if m.get("kind") == "keyword"]
        errors = [m.get("message") for m in messages if m.get("kind") == "error"]
        if result.returncode != 0 and not errors:
            # a crash the listener did not catch: show it rather than "nothing recognised"
            errors = [f"結束碼 {result.returncode}：{result.stderr.strip()[-300:] or '（沒有訊息）'}"]
        if errors:
            print(f"  gain x{gain:>3}: ✗ 辨識程式出錯，不是沒聽到：{errors[0]}")
        else:
            print(f"  gain x{gain:>3}: {labels or '（沒有辨識到指令）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
