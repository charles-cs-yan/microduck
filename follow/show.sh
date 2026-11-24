#!/usr/bin/env bash
# Show day, one command, run by the operator in an SSH terminal on the duck:
#
#   bash ~/microduck-follow/current/follow/show.sh            # camera + voice + live
#   bash ~/microduck-follow/current/follow/show.sh --no-voice # keyboard only
#   SHOW_DRY=1 bash …/show.sh                                 # rehearsal without moving (dry run)
#   bash …/show.sh --scripted --no-voice --command-pipe /tmp/microduck-voice
#        # no camera, no sudo: f / "Let's go" walks straight on, h / "Let's go home" turns and walks back
#
# 1. read-only preflight (battery, CPU, gate) — refuses at or below 7.0 V (the user's floor; the vendor says 7.5)
# 2. sudo once (password typed by the operator), stop mediad (it holds the camera)
# 3. run follow.app; it asks "yes" itself before anything moves
# 4. ALWAYS start mediad again on exit (trap), whatever happened
set -u
BASE=${FOLLOW_HOME:-$HOME/microduck-follow}
cd "$BASE/current" || { echo "找不到 $BASE/current；先部署"; exit 2; }

VOICE=--voice
SCRIPTED=""
EXTRA=()
# left_hip_roll reads ~15 °C hotter than it feels since the 11/24 fall (the user checked by hand):
# its own limit is 80 °C by default here; pass another --hot-joint to change it.
HOT=(--hot-joint left_hip_roll:80)
for arg in "$@"; do
  case "$arg" in
    --no-voice) VOICE="" ;;
    --scripted) SCRIPTED=1; EXTRA+=("$arg") ;;
    --hot-joint|--hot-joint=*) HOT=(); EXTRA+=("$arg") ;;
    *) EXTRA+=("$arg") ;;
  esac
done
EXTRA+=("${HOT[@]}")
LIVE=--live
[ -n "${SHOW_DRY:-}" ] && LIVE=""

echo "== 唯讀檢查"
health=$(robotctl health 2>&1)
echo "$health" | grep -E "robot |battery|cpu|motors"
volts=$(echo "$health" | sed -n 's/.*battery *\([0-9.]*\) V.*/\1/p' | head -1)
# Pass only on a well-formed number > 7.0; anything odd (empty, ".", an awk error) refuses.
if ! awk -v v="$volts" 'BEGIN{exit !(v ~ /^[0-9]+(\.[0-9]+)?$/ && v + 0 > 7.0)}'; then
  echo "✗ 電池 ${volts:-未知} V：不高於 7.0 V 或讀不到，先充電。"; exit 1
fi
if awk -v v="$volts" 'BEGIN{exit !(v + 0 < 7.5)}'; then
  echo "⚠ 電池 ${volts} V：低於原廠的 7.5 V，請盡快充電（7.0 V 會停）。"
fi
python3 -m follow.probe --seconds 4 | tail -7

if [ -n "$VOICE" ] && [ ! -x "$BASE/venv-voice/bin/python" ]; then
  echo "✗ 沒有語音環境（$BASE/venv-voice）；先跑 follow/voice/setup_venv.sh，或加 --no-voice"; exit 2
fi
if [ -z "$SCRIPTED" ] && [ ! -x "$HOME/microduck-vision/.venv/bin/python" ]; then
  echo "✗ 沒有視覺環境（~/microduck-vision/.venv）；先跑 follow/vision/setup_venv.sh"; exit 2
fi

echo
read -r -p "扶好鴨子後按 Enter 繼續（Ctrl-C 取消，什麼都不會改）" _
if [ -n "$SCRIPTED" ]; then
  # No camera, so mediad keeps running and no sudo is needed.
  exec python3 -m follow.app $VOICE $LIVE "${EXTRA[@]}"
fi
echo "== 需要 sudo 停 mediad（它占用相機）："
sudo -v || exit 1
# Keep the sudo timestamp fresh (default 15 min) so restoring mediad at the end never needs a
# password — the operator may have closed the terminal by then.
( while sleep 60; do sudo -n -v 2>/dev/null || exit; done ) &
KEEPALIVE=$!
restore() {
  kill "$KEEPALIVE" 2>/dev/null
  echo; echo "== 恢復 mediad"
  if sudo -n systemctl start mediad 2>/dev/null; then
    echo "mediad：$(systemctl is-active mediad)"
  else
    echo "✗ 無法自動恢復 mediad（sudo 已過期）。請手動執行：sudo systemctl start mediad"
  fi
}
trap restore EXIT
trap 'exit 130' INT TERM HUP   # make Ctrl-C / closed SSH / kill go through the EXIT trap too
sudo systemctl stop mediad
echo "mediad：$(systemctl is-active mediad)"

python3 -m follow.app --camera /dev/video0 $VOICE $LIVE --record "$BASE/show-$(date +%m%d-%H%M).jsonl" "${EXTRA[@]}"
