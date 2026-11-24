#!/usr/bin/env bash
# Vision environment on the duck for follow's camera worker: upstream vision_follow files + OpenCV.
# Needs internet (pip). No sudo, no robotd, no motors, no services touched.
#
# 1. From the laptop (upstream script, copies files only):  sh microduck/vision_follow/push.sh radxa@<duck>
# 2. On the duck:  bash ~/microduck-follow/current/follow/vision/setup_venv.sh
#
# Creates ~/microduck-vision/.venv (undo: rm -rf ~/microduck-vision/.venv). Wheels exist for
# aarch64 / CPython 3.13: opencv-contrib-python-headless 4.12.0.88 (abi3) with numpy 2.2.x.
set -euo pipefail
V=${VISION_DIR:-$HOME/microduck-vision}
PY=${PYTHON:-python3}
[ -f "$V/aruco_detector.py" ] || { echo "沒有 $V/aruco_detector.py；先在筆電跑 sh microduck/vision_follow/push.sh radxa@<鴨子>"; exit 2; }
if [ ! -x "$V/.venv/bin/python" ]; then
  if ! "$PY" -m venv "$V/.venv" 2>/dev/null; then
    echo "python3 -m venv 失敗（多半是缺 python3-venv）；改用 --without-pip + get-pip.py"
    rm -rf "$V/.venv"
    "$PY" -m venv --without-pip "$V/.venv"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$V/get-pip.py"
    "$V/.venv/bin/python" -I "$V/get-pip.py" -q
  fi
fi
export PIP_TIMEOUT=90 PIP_RETRIES=8
"$V/.venv/bin/python" -m pip install -q --only-binary=:all: -r "$V/requirements.txt"
"$V/.venv/bin/python" -c 'import cv2, numpy; assert hasattr(cv2, "aruco"); print("cv2", cv2.__version__, "numpy", numpy.__version__)'
"$V/.venv/bin/python" -c "import sys; sys.path.insert(0, '$V'); from aruco_detector import ArucoDetector; ArucoDetector(); print('ArucoDetector OK')"
echo "視覺環境就緒：$V/.venv/bin/python"
