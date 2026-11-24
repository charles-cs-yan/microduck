#!/usr/bin/env bash
# Voice environment for follow on the duck: sherpa-onnx + numpy only, and the English KWS model.
# Needs internet (pip + the model from k2-fsa's GitHub release). No sudo, no robotd, no motors.
#
#   bash ~/microduck-follow/current/follow/voice/setup_venv.sh
#
# Creates  ~/microduck-follow/venv-voice/          (undo: rm -rf ~/microduck-follow/venv-voice)
#          ~/microduck-follow/models/kws-gigaspeech/ (undo: rm -rf ~/microduck-follow/models)
# Versions follow upstream speech/requirements.txt (sherpa-onnx 1.13.8; numpy by Python version).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=${FOLLOW_HOME:-$HOME/microduck-follow}
PY=${PYTHON:-python3}

if [ ! -x "$BASE/venv-voice/bin/python" ]; then
  if ! "$PY" -m venv "$BASE/venv-voice" 2>/dev/null; then
    echo "python3 -m venv 失敗（多半是缺 python3-venv）；改用 --without-pip + get-pip.py"
    rm -rf "$BASE/venv-voice"
    "$PY" -m venv --without-pip "$BASE/venv-voice"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$BASE/get-pip.py"
    "$BASE/venv-voice/bin/python" -I "$BASE/get-pip.py" -q
  fi
fi
V="$BASE/venv-voice/bin/python"
export PIP_TIMEOUT=90 PIP_RETRIES=8
# click + sentencepiece: sherpa-onnx-cli (kws_prepare.py, only needed to recompile keywords) imports them.
"$V" -m pip install -q --only-binary=:all: "sherpa-onnx==1.13.8" \
  'numpy==1.26.4; python_version < "3.13"' 'numpy==2.2.6; python_version >= "3.13"' \
  sentencepiece==0.2.1 click==8.1.8
"$V" -c 'import numpy, sherpa_onnx; print("numpy", numpy.__version__, "sherpa-onnx", sherpa_onnx.__version__)'
"$V" "$HERE/fetch_model.py" --dir "$BASE/models/kws-gigaspeech"
"$V" "$HERE/kws_listen.py" --model-dir "$BASE/models/kws-gigaspeech" --check
echo "語音環境就緒：$V"
