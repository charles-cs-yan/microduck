"""Compile follow/voice/keywords_en.txt for a KWS model. Never touches robotd.

Runs with a Python that has sherpa-onnx (it needs sherpa-onnx-cli):

    PY follow/voice/kws_prepare.py                              # default: gigaspeech (English, BPE)
    PY follow/voice/kws_prepare.py --model-dir ~/mdev/kws-gigaspeech
    PY follow/voice/kws_prepare.py --model zh-en --speech-dir ~/microduck-speech/current

Writes keywords_en.<model>.compiled.txt (zh-en: keywords_en.compiled.txt, as before) next to this
file plus a manifest, so the listener can refuse a stale compile. A word the model cannot spell
makes text2token fail — then choose another phrasing rather than editing the model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from models import MODELS, raw_for  # noqa: E402

RAW = HERE / "keywords_en.txt"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_path(model: str) -> Path:
    return HERE / MODELS[model]["compiled"].replace(".compiled.txt", ".manifest.json")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gigaspeech", choices=sorted(MODELS))
    parser.add_argument("--model-dir", type=Path, help="gigaspeech: directory with tokens.txt and bpe.model")
    parser.add_argument("--speech-dir", type=Path, default=Path.home() / "microduck-speech" / "current",
                        help="zh-en: upstream speech release")
    args = parser.parse_args(argv)
    spec = MODELS[args.model]
    name = "sherpa-onnx-cli.exe" if sys.platform == "win32" else "sherpa-onnx-cli"
    cli = Path(sysconfig.get_path("scripts")) / name
    if not cli.is_file():
        print(f"找不到 {cli}；請用裝了 sherpa-onnx 的 Python 執行", file=sys.stderr)
        return 2
    compiled = HERE / spec["compiled"]
    if args.model == "gigaspeech":
        model_dir = args.model_dir or spec["default_dir"]
        extra = ["--tokens", str(model_dir / spec["files"]["tokens"]), "--tokens-type", "bpe",
                 "--bpe-model", str(model_dir / spec["files"]["bpe"])]
        model_id = spec["id"]
    else:
        catalog = json.loads((args.speech_dir / "catalog.json").read_text(encoding="utf-8"))
        model_id = catalog["default"]
        files = catalog["models"][model_id]
        model_dir = args.speech_dir / "models" / model_id
        extra = ["--tokens", str(model_dir / files["tokens"]), "--tokens-type", "phone+ppinyin",
                 "--lexicon", str(model_dir / files["lexicon"])]
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.txt"
        raw.write_text(raw_for(args.model, RAW.read_text(encoding="utf-8")), encoding="utf-8")
        subprocess.run([str(cli), "text2token", *extra, str(raw), str(compiled)], check=True)
    manifest_path(args.model).write_text(json.dumps(
        {"model_id": model_id, "raw_sha256": sha256(RAW), "compiled_sha256": sha256(compiled)}, indent=2),
        encoding="utf-8")
    print(f"已編譯：{compiled}（model {model_id}）")
    print(compiled.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
