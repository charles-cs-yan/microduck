"""Voice pipeline with real numpy / sherpa-onnx. Skipped unless they are importable.

    ~/mdev/speech-venv/bin/python -m unittest follow.tests.test_integration_voice
"""

import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VOICE = ROOT / "follow" / "voice"
SPEECH_DIR = Path(os.environ.get("FOLLOW_SPEECH_DIR", Path.home() / "mdev" / "speech"))

try:
    import numpy as np
    sys.path.insert(0, str(VOICE))
    import audio
    HAVE_NUMPY = True
except ImportError:  # pragma: no cover
    HAVE_NUMPY = False

try:
    import sherpa_onnx  # noqa: F401
    HAVE_SHERPA = HAVE_NUMPY and (SPEECH_DIR / "catalog.json").is_file()
except ImportError:  # pragma: no cover
    HAVE_SHERPA = False


def write_wav(path, frames, channels, width, rate=16000):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)


@unittest.skipUnless(HAVE_NUMPY, "needs numpy (speech or vision venv)")
class AudioTest(unittest.TestCase):
    def test_left_channel_of_s32_stereo(self):
        left = [2 ** 31 // 100, -(2 ** 31) // 100]      # ±0.01
        frames = b"".join(struct.pack("<ii", l, 0) for l in left)
        mono = audio.to_mono(frames, "S32_LE", 2, 0, 10.0)
        self.assertTrue(np.allclose(mono, [0.1, -0.1], atol=1e-6))
        self.assertTrue(np.all(audio.to_mono(frames, "S32_LE", 2, 1, 10.0) == 0), "right channel is silent")

    def test_gain_clips_and_partial_frames(self):
        frames = struct.pack("<ii", 2 ** 31 // 2, 0) + b"\x01\x02"  # trailing half frame ignored
        mono = audio.to_mono(frames, "S32_LE", 2, 0, 10.0)
        self.assertEqual(mono.tolist(), [1.0])

    def test_s16_mono_matches_old_behaviour(self):
        frames = struct.pack("<hh", 16384, -16384)
        self.assertTrue(np.allclose(audio.to_mono(frames, "S16_LE", 1, 0, 1.0), [0.5, -0.5]))
        with self.assertRaises(ValueError):
            audio.to_mono(frames, "S16_LE", 1, 1, 1.0)

    def test_read_wav_any_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            stereo32 = Path(tmp) / "s.wav"
            write_wav(stereo32, struct.pack("<ii", 2 ** 31 // 4, 0) * 10, 2, 4)
            samples, rate = audio.read_wav(stereo32, 0, 2.0)
            self.assertEqual(rate, 16000)
            self.assertTrue(np.allclose(samples, 0.5))
            mono16 = Path(tmp) / "m.wav"
            write_wav(mono16, struct.pack("<h", 8192) * 10, 1, 2)
            samples, _ = audio.read_wav(mono16, 0, 1.0)
            self.assertTrue(np.allclose(samples, 0.25))
        self.assertLess(audio.dbfs(np.full(100, 0.01, np.float32)), -39)


def wav_header(channels, rate, bits, extra_chunk=b""):
    block = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * block, block, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + extra_chunk + b"data" + struct.pack("<I", 0x7FFFFFFF)
    return b"RIFF" + struct.pack("<I", 0x7FFFFFFF) + body


@unittest.skipUnless(HAVE_NUMPY, "needs numpy")
class WavHeaderTest(unittest.TestCase):
    def test_arecord_style_header(self):
        header = wav_header(2, 16000, 32)
        self.assertEqual(audio.parse_wav_header(header + b"\x00" * 8), (44, 2, 16000, 32))

    def test_needs_more_bytes(self):
        header = wav_header(2, 16000, 32)
        for cut in (0, 8, 20, 30, 40):
            self.assertIsNone(audio.parse_wav_header(header[:cut]), cut)

    def test_extra_chunk_and_wrong_rate(self):
        listing = b"LIST" + struct.pack("<I", 5) + b"abcde" + b"\x00"  # odd size → pad byte
        parsed = audio.parse_wav_header(wav_header(2, 48000, 32, listing))
        self.assertEqual(parsed[1:], (2, 48000, 32))
        self.assertEqual(parsed[0], 44 + 14)

    def test_not_wav(self):
        with self.assertRaises(ValueError):
            audio.parse_wav_header(b"OggS" + b"\x00" * 40)


@unittest.skipUnless(HAVE_SHERPA, "needs sherpa-onnx and a prepared speech dir (~/mdev/speech)")
class LiveCaptureTest(unittest.TestCase):
    """kws_listen's live path with a fake `arecord` on PATH (no ALSA needed)."""

    def run_listener(self, payload: bytes, model_args):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "stream.bin"
            data.write_bytes(payload)
            fake = Path(tmp) / "arecord"
            fake.write_text(f"#!/bin/sh\ncat '{data}'\n")
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}")
            return subprocess.run([sys.executable, str(VOICE / "kws_listen.py"), *model_args],
                                  capture_output=True, text=True, timeout=120, env=env)

    def model_args(self):
        model_dir = os.environ.get("FOLLOW_KWS_MODEL_DIR")
        if not model_dir:
            self.skipTest("set FOLLOW_KWS_MODEL_DIR to an extracted gigaspeech model")
        return ["--model-dir", model_dir]

    def test_wrong_rate_fails_loudly(self):
        result = self.run_listener(wav_header(2, 48000, 32) + b"\x00" * 64000, self.model_args())
        self.assertIn("48000 Hz", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_right_format_is_decoded(self):
        tts = Path(os.environ.get("FOLLOW_TTS_WAV", ""))
        if not tts.is_file():
            self.skipTest("set FOLLOW_TTS_WAV")
        samples, rate = audio.read_wav(tts)
        quiet = (samples * 0.01 * 2 ** 31).astype("<i4")
        stereo = np.stack([quiet, np.zeros_like(quiet)], axis=1).tobytes()
        silence = np.zeros(32000 * 2, dtype="<i4").tobytes()
        result = self.run_listener(wav_header(2, 16000, 32) + stereo + silence, self.model_args())
        self.assertIn('"label": "follow"', result.stdout, result.stdout[-400:])


@unittest.skipUnless(HAVE_SHERPA, "needs sherpa-onnx and a prepared speech dir (~/mdev/speech)")
class ListenerWavTest(unittest.TestCase):
    def test_stereo_s32_wav_through_the_listener(self):
        """The listener decodes a native-format recording (left channel, gain applied)."""
        tts = Path(os.environ.get("FOLLOW_TTS_WAV", ""))
        if not tts.is_file():
            self.skipTest("set FOLLOW_TTS_WAV to a mono 16-bit 'follow me' recording")
        samples, rate = audio.read_wav(tts)
        quiet = (samples * 0.01 * 2 ** 31).astype("<i4")  # ≈ -40 dBFS like the duck's mic
        stereo = np.stack([quiet, np.zeros_like(quiet)], axis=1).tobytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duck_like.wav"
            write_wav(path, stereo, 2, 4, rate)
            out = subprocess.run([sys.executable, str(VOICE / "kws_listen.py"), "--model", "zh-en",
                                  "--speech-dir", str(SPEECH_DIR), "--wav", str(path), "--gain", "30"],
                                 capture_output=True, text=True, timeout=120)
        self.assertIn('"label": "follow"', out.stdout, out.stdout[-500:] + out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
