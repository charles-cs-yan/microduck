#!/usr/bin/env bash
# Operator console for the boot standby (follow/standby.sh). Works from the laptop or from a phone
# SSH app (Termius and the like) — no laptop needed for keys:
#
#   ssh -t radxa@<duck> bash ~/microduck-follow/current/follow/console.sh
#
# 1. Type `yes` + Enter to unlock (hold the duck first). Line mode, so a stray tap cannot unlock.
# 2. Then single keys, no Enter: f d h k w g x u p 1 2 3 4 s — the same keys as show.sh.
#    Space or Enter = stop.  q = leave the console only (the duck stops, the show keeps waiting).
#    Q = end the show (standby.sh --stop).
# If this SSH session drops (phone locked, Wi-Fi gone), the show sees the console go away and stops
# the duck; connect again, type yes, carry on.
set -u
BASE=${FOLLOW_HOME:-$HOME/microduck-follow}
HERE=$(cd "$(dirname "$0")" && pwd)
LOG="$BASE/standby.log"
KEYS=/tmp/microduck-keys

if ! bash "$HERE/standby.sh" --status | grep -q 待命中; then
  echo "開機待命沒有在跑，現在啟動："
  bash "$HERE/standby.sh"
fi
for _ in $(seq 20); do [ -p "$KEYS" ] && break; sleep 0.5; done
[ -p "$KEYS" ] || { echo "✗ 等不到 $KEYS（show 沒起來？看 $LOG）"; exit 2; }

tail -n 15 -F "$LOG" 2>/dev/null &
TAIL=$!
STTY=$(stty -g 2>/dev/null || true)
cleanup() { kill "$TAIL" 2>/dev/null; [ -n "$STTY" ] && stty "$STTY" 2>/dev/null; }
trap cleanup EXIT
# Opening a FIFO for writing blocks until the show is reading it; it is from the start.
exec 3>"$KEYS"
sleep 0.3
send() { printf '%s\n' "$1" >&3 || { echo "✗ show 不在了"; exit 2; }; }
end_show() { exec 3>&-; bash "$HERE/standby.sh" --stop; exit 0; }

echo
echo "== 扶好鴨子，輸入 yes 再按 Enter 解鎖（q 離開，Q 結束 show）"
while IFS= read -r line; do
  case "$line" in
    yes) send yes; break ;;
    q) echo "離開操作台"; exit 0 ;;
    Q) end_show ;;
    *) echo "還沒解鎖：輸入 yes 再按 Enter" ;;
  esac
done || exit 0

cat <<'HELP'
== 已解鎖：直接按鍵，不用 Enter
   f 上台  d 跳舞  h 回家  k 直走
   u 站起  x 坐下  w 打招呼  p 趴下
   空白/Enter = 停   q 離開   Q 結束
HELP
while IFS= read -rsn1 key; do
  case "$key" in
    ""|" ") send ""; echo "→ 停" ;;
    q) echo "離開操作台（鴨子停下，show 繼續待命）"; exit 0 ;;
    Q) end_show ;;
    *) send "$key"; echo "→ $key" ;;
  esac
done
