"""Keyword-spotting models follow can use. Standard library only.

2026-11-23 comparison on the duck's real microphone (left channel, ~-45 dBFS speech, ~3-4 dB
above the duck's own noise):
  * zh-en-3m (upstream speech/ default, phones + pinyin): synthetic English fine, but the
    host's real "follow me" never fired, not even at threshold 0.05 / score 3 or with
    pronunciation variants.
  * gigaspeech-3.3m (sherpa-onnx official English KWS, BPE): the same real recording fires at
    every setting tried; 6 synthetic commands correct; no false triggers on silent recordings,
    or on 6 distractor sentences mixed with the duck's noise; 4 of 5 commands found at
    duck-like level in duck noise ("let's go home" missed).
So gigaspeech is the default; zh-en stays selectable.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Not relative to this file: deployed code lives in ~/microduck-follow/releases/<stamp>/follow/.
FOLLOW_HOME = Path(os.environ.get("FOLLOW_HOME", Path.home() / "microduck-follow"))

MODELS = {
    "gigaspeech": {
        "id": "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
               "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2",
        # Recorded from the official release on 2026-11-23 (17,626,723 bytes); not an upstream checksum.
        "sha256": "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a",
        "files": {
            "tokens": "tokens.txt",
            "bpe": "bpe.model",
            "encoder": "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder": "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
            "joiner": "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        },
        "tokens_type": "bpe",
        "trailing_blanks": 1,
        "compiled": "keywords_en.gigaspeech.compiled.txt",
        "default_dir": FOLLOW_HOME / "models" / "kws-gigaspeech",
    },
    "zh-en": {
        "id": "zh-en-kws-3m-20251220",
        "tokens_type": "phone+ppinyin",
        "trailing_blanks": 2,
        "compiled": "keywords_en.compiled.txt",
        # files come from upstream speech/catalog.json in --speech-dir
    },
}


def raw_for(model: str, text: str) -> str:
    """The BPE vocabulary has no apostrophe: LET'S → LETS (same sound)."""
    return text.replace("LET'S", "LETS") if MODELS[model]["tokens_type"] == "bpe" else text
