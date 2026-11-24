"""Score a recognizer on a guided voice session (voice_session.py): hits per phrase, misfires.

Laptop-side, needs sherpa-onnx and numpy. Two kinds of recognizer:

    # keyword spotter (what the duck runs today)
    PY voice_eval.py SESSION --kws zh-en MODEL_DIR KEYWORDS_FILE [--threshold 0.25]
    # streaming ASR + loose word matching (follow/voice/intent.py)
    PY voice_eval.py SESSION --asr MODEL_DIR

A detection counts for prompt n when it starts between the prompt's time and the next
prompt's time + 1.5 s (people answer late). "none" prompts must stay silent.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import wave
from collections import defaultdict
from pathlib import Path
from typing import List, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from intent import intent_of  # noqa: E402


def load_left(path: Path, gain: float) -> np.ndarray:
    with wave.open(str(path)) as w:
        ch, sw = w.getnchannels(), w.getsampwidth()
        x = np.frombuffer(w.readframes(w.getnframes()), {2: "<i2", 4: "<i4"}[sw]).astype(np.float32)
    x = x.reshape(-1, ch)[:, 0] / float(2 ** (8 * sw - 1))
    if sw == 4:  # the duck's raw S32 capture: apply the listener's digital gain
        x = np.clip(x * gain, -1, 1)
    x[:8000] = 0  # start-up click
    return x


def run_kws(x, model_dir: Path, keywords: Path, threshold: float, kind: str) -> List[Tuple[float, str, str]]:
    import sherpa_onnx
    pick = lambda pat: sorted(glob.glob(str(model_dir / pat)))[0]  # noqa: E731
    kws = sherpa_onnx.KeywordSpotter(
        tokens=str(model_dir / "tokens.txt"), encoder=pick("encoder*.onnx"), decoder=pick("decoder*.onnx"),
        joiner=pick("joiner*.onnx"), keywords_file=str(keywords), num_threads=2,
        keywords_threshold=threshold, keywords_score=1.0, num_trailing_blanks=2 if kind == "zh-en" else 1,
        provider="cpu")
    st, out = kws.create_stream(), []
    for i in range(0, len(x), 1600):
        st.accept_waveform(16000, x[i:i + 1600])
        while kws.is_ready(st):
            kws.decode_stream(st)
            r = kws.get_result(st)
            if r:
                out.append((i / 16000, r.strip(), r.strip()))
                kws.reset_stream(st)
    return out


def run_asr(x, model_dir: Path) -> List[Tuple[float, str, str]]:
    import sherpa_onnx
    def pick(pat):
        c = sorted(glob.glob(str(model_dir / pat)))
        return ([p for p in c if "int8" in p] or c)[0]
    rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(model_dir / "tokens.txt"), encoder=pick("encoder*.onnx"), decoder=pick("decoder*.onnx"),
        joiner=pick("joiner*.onnx"), num_threads=2, sample_rate=16000, feature_dim=80,
        enable_endpoint_detection=True, rule1_min_trailing_silence=1.0, rule2_min_trailing_silence=0.5,
        decoding_method="greedy_search", provider="cpu")
    st, out, start = rec.create_stream(), [], None
    tail = np.zeros(16000, np.float32)
    for i in range(0, len(x) + len(tail), 1600):
        chunk = x[i:i + 1600] if i < len(x) else tail[:1600]
        st.accept_waveform(16000, chunk)
        while rec.is_ready(st):
            rec.decode_stream(st)
        text = rec.get_result(st).strip()
        if text and start is None:
            start = i / 16000
        if rec.is_endpoint(st):
            if text:
                out.append((start if start is not None else i / 16000, intent_of(text) or "-", text))
            rec.reset(st)
            start = None
    return out


def score(prompts, hits, gap: float):
    """Assign each detection to the prompt whose window it falls in; return per-label tallies."""
    tally = defaultdict(lambda: [0, 0])  # label -> [heard right, asked]
    wrong, rows = [], []
    for k, p in enumerate(prompts):
        lo = p["at"]
        hi = (prompts[k + 1]["at"] if k + 1 < len(prompts) else p["at"] + gap) + 1.5
        got = [(t, lab, txt) for t, lab, txt in hits if lo <= t < hi and lab != "-"]
        heard = [lab for _, lab, _ in got]
        texts = [txt for t, _lab, txt in hits if lo <= t < hi]
        ok = (not heard) if p["label"] == "none" else (p["label"] in heard or
                                                       (p["label"] == "home" and "letsgo" in heard))
        tally[p["label"]][1] += 1
        tally[p["label"]][0] += ok
        extra = [h for h in heard if h != p["label"] and not (p["label"] == "home" and h == "letsgo")
                 and not (p["label"] == "follow" and h == "letsgo")]
        if extra:
            wrong.append(f"#{p['n']} {p['text']!r} → {extra}")
        rows.append(f"#{p['n']:2d} {'✓' if ok else '✗'} {p['text']:<28} heard {heard or '-'}  {texts if texts else ''}")
    return tally, wrong, rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session", type=Path)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--kws", nargs=3, metavar=("KIND", "MODEL_DIR", "KEYWORDS"))
    g.add_argument("--asr", type=Path, metavar="MODEL_DIR")
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--gain", type=float, default=20.0)
    ap.add_argument("--rows", action="store_true", help="print every prompt")
    a = ap.parse_args(argv)
    meta = json.loads((a.session / "prompts.json").read_text(encoding="utf-8"))
    x = load_left(a.session / "session.wav", a.gain)
    if a.kws:
        kind, md, kw = a.kws
        hits = run_kws(x, Path(md), Path(kw), a.threshold, kind)
    else:
        hits = run_asr(x, a.asr)
    tally, wrong, rows = score(meta["prompts"], hits, meta.get("gap", 4.0))
    total = sum(v[0] for k, v in tally.items() if k != "none"), sum(v[1] for k, v in tally.items() if k != "none")
    print(f"命中 {total[0]}/{total[1]}  " + "  ".join(f"{k}:{v[0]}/{v[1]}" for k, v in sorted(tally.items())))
    if wrong:
        print("  聽錯：" + "；".join(wrong))
    if a.rows:
        print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
