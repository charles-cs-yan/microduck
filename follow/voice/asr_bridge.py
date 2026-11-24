"""Laptop-side voice: hear the duck's microphone over SSH, recognise speech, send commands back.

    python asr_bridge.py --duck radxa@192.168.50.237 --model ~/mdev/asr/sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06

The duck's CPU is too slow (and throttles when hot) for a full recogniser, and its small keyword
spotters miss too much of real, natural speech. So the duck only streams raw audio (arecord over SSH,
~128 kB/s) and the laptop runs a streaming zipformer ASR; follow/voice/intent.py maps what was
said to a command label, which goes back over a second SSH into the app's --command-pipe.

Nothing here talks to robotd: the app's CommandQueue and safety gates decide what happens.
Start the show with:  bash show.sh --no-voice --command-pipe /tmp/microduck-voice
"""

from __future__ import annotations

import argparse
import glob
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from intent import intent_of  # noqa: E402

RATE = 16000
FRAME = 1600  # 0.1 s


def ssh_cmd() -> str:
    # Under WSL prefer Windows' own ssh: it has the laptop's key and known_hosts.
    for c in ("/mnt/c/Windows/System32/OpenSSH/ssh.exe", "ssh"):
        if shutil.which(c) or Path(c).exists():
            return c
    raise SystemExit("找不到 ssh")


def recogniser(model_dir: Path, threads: int):
    import sherpa_onnx

    def pick(pat):
        c = sorted(glob.glob(str(model_dir / pat)))
        if not c:
            raise SystemExit(f"{model_dir} 裡沒有 {pat}")
        return ([p for p in c if "int8" in p] or c)[0]
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(model_dir / "tokens.txt"), encoder=pick("encoder*.onnx"), decoder=pick("decoder*.onnx"),
        joiner=pick("joiner*.onnx"), num_threads=threads, sample_rate=RATE, feature_dim=80,
        enable_endpoint_detection=True, rule1_min_trailing_silence=1.0, rule2_min_trailing_silence=0.5,
        decoding_method="greedy_search", provider="cpu")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--duck", required=True, help="user@host of the duck")
    ap.add_argument("--ssh-opt", action="append", default=[], help="extra ssh option, e.g. HostKeyAlias=192.168.50.135")
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--mic", default="hw:microducki2s3,0")
    ap.add_argument("--gain", type=float, default=20.0)
    ap.add_argument("--pipe", default="/tmp/microduck-voice", help="the app's --command-pipe on the duck")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--cooldown", type=float, default=2.0, help="s before the same command may fire again")
    a = ap.parse_args(argv)

    ssh = [ssh_cmd(), "-o", "BatchMode=yes", "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=3"]
    for o in a.ssh_opt:
        ssh += ["-o", o]
    rec = recogniser(a.model.expanduser(), a.threads)
    print("模型載入完成", flush=True)
    last_fired = {}
    try:
        while True:  # reconnect whenever the duck, the show or the network drops out
            outcome = listen(a, ssh, rec, last_fired)
            print(f"  …{outcome}；3 秒後重新連線（Ctrl-C 結束）", flush=True)
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n結束。")
        return 0


def listen(a, ssh, rec, last_fired) -> str:
    """One connection: stream the mic, send labels. Returns why it ended."""
    # The command writer waits (up to 10 min) for the show to create its pipe, then `cat` blocks
    # until the app opens it for reading — so the bridge may be started before or after the show.
    wait_pipe = (f"for i in $(seq 300); do test -p {a.pipe} && pgrep -f -- '--command-pipe {a.pipe}' >/dev/null "
                 f"&& break; sleep 2; done; test -p {a.pipe} || exit 3; exec cat > {a.pipe}")
    sender = subprocess.Popen(ssh + [a.duck, wait_pipe], stdin=subprocess.PIPE, text=True)
    audio = subprocess.Popen(ssh + [a.duck, f"exec arecord -q -D {a.mic} -t raw -f S32_LE -c 2 -r {RATE}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(1.5)
        if audio.poll() is not None:
            err = audio.stderr.read().decode(errors="replace").strip()
            return f"收不到鴨子的麥克風（{err or '連不上'}；show 要用 --no-voice，麥克風才空著）"
        print("🎤 在聽了（show 還沒開也沒關係，開了就會接上）", flush=True)
        stream = rec.create_stream()
        fired_this_utterance = set()
        chunk_bytes = FRAME * 2 * 4
        while True:
            raw = audio.stdout.read(chunk_bytes)
            if not raw or len(raw) < chunk_bytes:
                return "麥克風串流中斷（網路或鴨子）"
            x = np.frombuffer(raw, "<i4").astype(np.float32).reshape(-1, 2)[:, 0] / 2 ** 31
            stream.accept_waveform(RATE, np.clip(x * a.gain, -1, 1))
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            text = rec.get_result(stream).strip()
            label = intent_of(text) if text else None
            # Fire on the partial result as soon as the deciding word appears (lower latency), but
            # only once per utterance; "letsgo" waits for the end so "let's go home/follow me" can win.
            endpoint = rec.is_endpoint(stream)
            if label and label not in fired_this_utterance and (label != "letsgo" or endpoint):
                if time.monotonic() - last_fired.get(label, -1e9) >= a.cooldown:
                    if sender.poll() is not None:
                        print(f"  ✗ 聽到 {text!r} → {label}，但 show 沒在跑，指令沒送出", flush=True)
                        return "show 結束了"
                    try:
                        sender.stdin.write(label + "\n")
                        sender.stdin.flush()
                    except (BrokenPipeError, OSError):
                        return "送指令的連線斷了"
                    last_fired[label] = time.monotonic()
                    print(f"  ✓ {text!r} → {label}", flush=True)
                fired_this_utterance.add(label)
            if endpoint:
                if text and not label:
                    print(f"  · {text!r}（不是指令）", flush=True)
                rec.reset(stream)
                fired_this_utterance.clear()
    finally:
        for p in (audio, sender):
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    sys.exit(main())
