"""PCM helpers for the duck's I2S microphone. Needs numpy (speech venv).

Measured on the duck (2026-11-23): `hw:microducki2s3,0` offers 2..64 channels and S16/S24/S32;
only the LEFT channel (0) carries the MEMS microphone, the right one is exactly zero, and a
voice at conversational distance sits around -45 dBFS (no analogue pre-gain). Recording mono
through `plughw` averaged the silent channel in (−6 dB) on top of that. So: capture the native
S32 stereo stream, keep channel 0, and apply a digital gain before the keyword spotter.
"""

from __future__ import annotations

import wave
from typing import Tuple

import numpy as np

FORMATS = {"S16_LE": ("<i2", 2 ** 15), "S32_LE": ("<i4", 2 ** 31)}


def frame_bytes(fmt: str, channels: int) -> int:
    return np.dtype(FORMATS[fmt][0]).itemsize * channels


def to_mono(data: bytes, fmt: str, channels: int, channel: int, gain: float) -> np.ndarray:
    """Interleaved PCM → one channel as float32 in [-1, 1], times gain, clipped."""
    dtype, scale = FORMATS[fmt]
    if not 0 <= channel < channels:
        raise ValueError(f"channel {channel} not in 0..{channels - 1}")
    usable = len(data) // frame_bytes(fmt, channels) * frame_bytes(fmt, channels)
    samples = np.frombuffer(data[:usable], dtype=dtype).reshape(-1, channels)[:, channel]
    return np.clip(samples.astype(np.float32) * np.float32(gain / scale), -1.0, 1.0)


def read_wav(path: str, channel: int = 0, gain: float = 1.0) -> Tuple[np.ndarray, int]:
    """Any 16/32-bit PCM WAV, mono or multi-channel → (float32 mono, rate)."""
    with wave.open(str(path), "rb") as source:
        width, channels, rate = source.getsampwidth(), source.getnchannels(), source.getframerate()
        fmt = {2: "S16_LE", 4: "S32_LE"}.get(width)
        if fmt is None:
            raise ValueError(f"只支援 16 或 32 位元 WAV，這個是 {width * 8} 位元")
        return to_mono(source.readframes(source.getnframes()), fmt, channels, min(channel, channels - 1), gain), rate


def parse_wav_header(buffer: bytes):
    """Parse the header arecord writes at the start of a `-t wav` stream.

    Returns (header_length, channels, rate, bits) once the `data` chunk header is in `buffer`,
    None if more bytes are needed. Raises ValueError for something that is not PCM WAV.
    The rate is what the hardware actually granted: arecord warns about an inexact rate on
    stderr (which nobody sees here) but writes the real value into this header.
    """
    import struct
    if len(buffer) < 12:
        return None
    if buffer[:4] != b"RIFF" or buffer[8:12] != b"WAVE":
        raise ValueError("arecord 輸出不是 WAV")
    offset, fmt = 12, None
    while True:
        if len(buffer) < offset + 8:
            return None
        chunk, size = buffer[offset:offset + 4], struct.unpack_from("<I", buffer, offset + 4)[0]
        if chunk == b"fmt ":
            if len(buffer) < offset + 8 + 16:
                return None
            tag, channels, rate = struct.unpack_from("<HHI", buffer, offset + 8)
            bits = struct.unpack_from("<H", buffer, offset + 22)[0]
            if tag not in (1, 0xFFFE):
                raise ValueError(f"WAV 不是 PCM（format tag {tag}）")
            fmt = (channels, rate, bits)
        elif chunk == b"data":
            if fmt is None:
                raise ValueError("WAV 的 data 出現在 fmt 之前")
            return (offset + 8, *fmt)
        offset += 8 + size + (size & 1)
        if offset > 4096:
            raise ValueError("WAV 檔頭太長")


def dbfs(samples: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) if samples.size else 0.0
    return 20 * np.log10(max(rms, 1e-9))
