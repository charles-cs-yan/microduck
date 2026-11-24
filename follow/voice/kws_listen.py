"""English keyword listener: microphone → JSON lines on stdout. No robot socket, no motors.

Runs in the speech environment (sherpa-onnx + numpy), started by follow/voice.py:

    {"kind": "keyword", "label": "follow", "t": 123.4}
    {"kind": "heartbeat", "t": 123.5}      # only after audio actually went through the decoder

Default model: sherpa-onnx's English GigaSpeech KWS (see voice/models.py for why; the upstream
zh-en model is still available with --model zh-en). Capture: the I2S card's native S32 stereo
stream, left channel, digital gain (see voice/audio.py). One thread, like upstream
speech/recognize.py. `--wav` replays a file instead of the microphone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent


def emit(kind: str, **fields) -> None:
    print(json.dumps({"kind": kind, "t": round(time.monotonic(), 3), **fields}), flush=True)


def check_compiled(model: str, model_id: str) -> Path:
    sys.path.insert(0, str(HERE))
    from models import MODELS
    compiled = HERE / MODELS[model]["compiled"]
    manifest_file = HERE / MODELS[model]["compiled"].replace(".compiled.txt", ".manifest.json")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    raw = hashlib.sha256((HERE / "keywords_en.txt").read_bytes()).hexdigest()
    if manifest.get("model_id") != model_id or manifest.get("raw_sha256") != raw:
        raise ValueError(f"keywords_en.txt 改過或模型不同；先重跑 kws_prepare.py --model {model}")
    if manifest.get("compiled_sha256") != hashlib.sha256(compiled.read_bytes()).hexdigest():
        raise ValueError(f"{compiled.name} 與 manifest 不符；重跑 kws_prepare.py --model {model}")
    return compiled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gigaspeech", choices=("gigaspeech", "zh-en"))
    parser.add_argument("--model-dir", type=Path, help="gigaspeech files (default ~/microduck-follow/models/kws-gigaspeech)")
    parser.add_argument("--speech-dir", type=Path, default=Path.home() / "microduck-speech" / "current",
                        help="zh-en: upstream speech release")
    parser.add_argument("--device", default="hw:microducki2s3,0",
                        help="ALSA input; the native hw node, not plughw (see voice/audio.py)")
    parser.add_argument("--format", default="S32_LE", choices=("S16_LE", "S32_LE"))
    parser.add_argument("--channels", type=int, default=2, help="the I2S card needs at least 2")
    parser.add_argument("--channel", type=int, default=0, help="the MEMS mic is on the left channel")
    parser.add_argument("--gain", type=float, default=20.0,
                        help="digital gain; speech arrives near -45 dBFS with no analogue pre-gain")
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--keywords-score", type=float, default=1.0)
    parser.add_argument("--wav", type=Path, help="replay a WAV (mono or stereo, 16/32-bit) instead of the mic")
    parser.add_argument("--check", action="store_true", help="load model + keywords, then exit")
    args = parser.parse_args(argv)
    if not 0 < args.gain <= 100 or not 0 < args.threshold <= 1 or not 0 < args.keywords_score <= 10:
        parser.error("gain must be 0..100, threshold 0..1, keywords-score 0..10")

    import numpy as np
    import sherpa_onnx
    sys.path.insert(0, str(HERE))
    from audio import frame_bytes, parse_wav_header, read_wav, to_mono

    from models import MODELS
    if args.model == "gigaspeech":
        spec = MODELS["gigaspeech"]
        model_id, files = spec["id"], spec["files"]
        model = args.model_dir or spec["default_dir"]
        from fetch_model import verify_installed
        problem = verify_installed(model)
        if problem:
            raise ValueError(f"{problem}；先跑 follow/voice/fetch_model.py")
    else:
        catalog = json.loads((args.speech_dir / "catalog.json").read_text(encoding="utf-8"))
        model_id = catalog["default"]
        files = catalog["models"][model_id]
        model = args.speech_dir / "models" / model_id
    keywords = check_compiled(args.model, model_id)
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(model / files["tokens"]), encoder=str(model / files["encoder"]),
        decoder=str(model / files["decoder"]), joiner=str(model / files["joiner"]),
        keywords_file=str(keywords), num_threads=1, keywords_threshold=args.threshold,
        keywords_score=args.keywords_score, num_trailing_blanks=MODELS[args.model]["trailing_blanks"],
        provider="cpu")
    emit("ready", model=model_id, device=args.device, channel=args.channel, gain=args.gain)
    if args.check:
        return 0
    stream = spotter.create_stream()
    next_heartbeat = 0.0

    def decode(samples, rate):
        nonlocal next_heartbeat
        stream.accept_waveform(rate, samples)
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            result = spotter.get_result(stream)
            if result:
                emit("keyword", label=result.strip())
                spotter.reset_stream(stream)
        now = time.monotonic()
        if now >= next_heartbeat:
            emit("heartbeat")
            next_heartbeat = now + 0.25

    if args.wav:
        samples, rate = read_wav(str(args.wav), args.channel, args.gain)
        step = max(1, rate // 10)
        for start in range(0, len(samples), step):
            decode(samples[start:start + step], rate)
        decode(np.zeros(rate, dtype="float32"), rate)
        return 0

    frame = frame_bytes(args.format, args.channels)
    want_bits = {"S16_LE": 16, "S32_LE": 32}[args.format]
    # `-t wav` rather than raw: the header carries the rate the hardware really granted, so a
    # node that cannot do 16 kHz fails loudly here instead of feeding the spotter the wrong rate.
    process = subprocess.Popen(["arecord", "-q", "-D", args.device, "-t", "wav", "-f", args.format,
                                "-r", "16000", "-c", str(args.channels)], stdout=subprocess.PIPE)
    try:
        pending, last_data, header_done = b"", time.monotonic(), False
        while True:
            if not select.select([process.stdout], [], [], 0.2)[0]:
                if time.monotonic() - last_data > 3.0:
                    raise RuntimeError("麥克風 3 秒沒有資料；檢查 ALSA 裝置或是否被占用")
                continue
            chunk = os.read(process.stdout.fileno(), 1600 * frame)
            if not chunk:
                raise RuntimeError(f"arecord 結束（{process.poll()}）；檢查裝置與麥克風占用")
            last_data = time.monotonic()
            pending += chunk
            if not header_done:
                parsed = parse_wav_header(pending)
                if parsed is None:
                    continue
                length, channels, rate, bits = parsed
                if (channels, rate, bits) != (args.channels, 16000, want_bits):
                    raise RuntimeError(f"麥克風實際格式 {channels} 聲道 {rate} Hz {bits} 位元，"
                                       f"不是要求的 {args.channels} 聲道 16000 Hz {want_bits} 位元")
                pending, header_done = pending[length:], True
            complete = len(pending) // frame * frame
            if complete:
                decode(to_mono(pending[:complete], args.format, args.channels, args.channel, args.gain), 16000)
                pending = pending[complete:]
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError, KeyError,
            wave.Error) as error:
        emit("error", message=f"{type(error).__name__}: {error}")
        sys.exit(1)
