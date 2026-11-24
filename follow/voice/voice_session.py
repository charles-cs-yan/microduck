"""Guided voice recording on the duck: prompts each show phrase several times and records it all.

    ~/microduck-follow/venv-voice/bin/python ~/microduck-follow/current/follow/voice/voice_session.py [--takes 5] [--speaker boss]

No sudo, no motors. Writes ~/microduck-follow/voice-sessions/<speaker>-<time>/{session.wav, prompts.json}.
prompts.json lists, for every prompt, the phrase, its label and the second it was shown, so
follow/voice/voice_eval.py (on the laptop) can score any model / keyword list against it.
Stop the show (and anything else using the mic) first: the ALSA hw node has one reader.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from models import FOLLOW_HOME  # noqa: E402

# (label, what the speaker reads). The show's phrases first, then the rest of the vocabulary.
PHRASES = [
    ("follow", "Let's go, follow me!"),
    ("dance", "Let's party!"),
    ("home", "Let's go home"),
    ("letsgo", "Let's go"),
    ("dance", "Shake it!"),
    ("greet", "Say hello"),
    ("gaze", "Look at me"),
    ("stop", "Stop"),
    ("none", "(something else: e.g. 'Thank you everyone, welcome to the show')"),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="hw:microducki2s3,0")
    parser.add_argument("--takes", type=int, default=5, help="times each phrase is asked for")
    parser.add_argument("--gap", type=float, default=4.0, help="seconds per prompt")
    parser.add_argument("--speaker", default="speaker", help="name for the folder, e.g. boss")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    if not 1 <= args.takes <= 20 or not 2.5 <= args.gap <= 10:
        parser.error("--takes 1..20, --gap 2.5..10")

    order = [p for p in PHRASES for _ in range(args.takes)]
    random.Random(args.seed).shuffle(order)  # mixed order, like a real show, not five in a row
    out = FOLLOW_HOME / "voice-sessions" / f"{args.speaker}-{time.strftime('%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    total = 3 + len(order) * args.gap + 2
    print(f"共 {len(order)} 句，約 {total / 60:.1f} 分鐘。站在鴨子前面約 1 公尺、面向它，用平常對老闆說話的音量。")
    print("看到「>>> 說：...」就說一次，說完等下一句。不用很快。Ctrl-C 隨時停止（已錄的會保留）。")
    input("準備好按 Enter 開始錄音 ")
    rec = subprocess.Popen(["arecord", "-q", "-D", args.device, "-f", "S32_LE", "-c", "2", "-r", "16000",
                            str(out / "session.wav")], stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    if rec.poll() is not None:  # the mic is busy (a show or listener still running): stop before asking anyone to talk
        print(f"✗ 錄音沒開始：{(rec.stderr.read() or '').strip()}")
        print("  先結束 show / follow.app（它們佔著麥克風），再跑一次。")
        return 1
    t0 = time.monotonic()
    prompts = []
    try:
        time.sleep(3)  # let the recording settle; the first second has a start-up click
        for n, (label, text) in enumerate(order, 1):
            at = time.monotonic() - t0
            prompts.append({"n": n, "label": label, "text": text, "at": round(at, 2)})
            print(f"\n[{n:2d}/{len(order)}] >>> 說：{text}", flush=True)
            time.sleep(args.gap)
        time.sleep(2)
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        rec.send_signal(signal.SIGINT)
        try:
            rec.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rec.kill()
        (out / "prompts.json").write_text(json.dumps(
            {"speaker": args.speaker, "gap": args.gap, "rate": 16000, "prompts": prompts},
            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完成：{out}")
    print("筆電上：scp -r radxa@<duck>:" + str(out).replace(str(Path.home()), "~") + " .  再跑 voice_eval.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
