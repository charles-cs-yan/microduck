#!/usr/bin/env bash
# Boot standby: start the scripted show with no terminal, so it is already waiting when the
# laptop connects. Started at boot by the user crontab line follow/standby_install.sh adds.
#
#   bash ~/microduck-follow/current/follow/standby.sh            # start (no-op if already running)
#   bash ~/microduck-follow/current/follow/standby.sh --stop     # end it (the duck stays as it is)
#   bash ~/microduck-follow/current/follow/standby.sh --status
#
# Nothing moves at boot: robotd starts limp, and follow.app waits for the console
# (follow/console.sh) to send "yes" before it even connects to robotd. Keys then come down
# /tmp/microduck-keys, the laptop's voice down /tmp/microduck-voice. A console or voice link that
# drops queues a stop. Log: ~/microduck-follow/standby.log
set -u
BASE=${FOLLOW_HOME:-$HOME/microduck-follow}
LOG="$BASE/standby.log"
KEYS=/tmp/microduck-keys
VOICE=/tmp/microduck-voice
WALK_M=${STANDBY_WALK_M:-2}
PATTERN='follow.app .*--standby'

running() { pgrep -u "$(id -u)" -f -- "$PATTERN" >/dev/null; }

case "${1:-}" in
  --status)
    if running; then echo "待命中（pid $(pgrep -u "$(id -u)" -f -- "$PATTERN" | tr '\n' ' ')）"; else echo "沒有在跑"; fi
    exit 0 ;;
  --stop)
    # SIGTERM → KeyboardInterrupt → MotionGuard sends robot.stop on the way out.
    pkill -TERM -u "$(id -u)" -f -- "$PATTERN" && echo "已送出結束（鴨子停下，姿勢不變）" || echo "沒有在跑"
    pkill -TERM -u "$(id -u)" -f -- 'follow/standby.sh --loop' 2>/dev/null
    exit 0 ;;
  --loop) ;;
  "")
    if running; then echo "已經在待命"; exit 0; fi
    nohup bash "$0" --loop >>"$LOG" 2>&1 </dev/null &
    echo "開機待命已啟動；紀錄：$LOG"
    exit 0 ;;
  *) echo "用法：standby.sh [--stop|--status]"; exit 2 ;;
esac

# ---- --loop: the long-running part, output goes to $LOG
exec 9>"/tmp/microduck-standby.lock"
flock -n 9 || { echo "$(date '+%F %T') 另一個 standby 已在跑，結束"; exit 0; }
echo "$(date '+%F %T') == standby 啟動"
cd "$BASE/current" || { echo "找不到 $BASE/current"; exit 2; }

# robotd comes up a little after cron's @reboot: wait for its socket (up to 3 min).
for _ in $(seq 90); do [ -S /run/robotd.sock ] && break; sleep 2; done
[ -S /run/robotd.sock ] || { echo "✗ 3 分鐘內等不到 /run/robotd.sock，放棄"; exit 2; }

for attempt in 1 2 3; do
  # Battery floor, as show.sh: refuse at or below 7.0 V or when it cannot be read. MotionGuard keeps
  # checking while it runs; this only stops a flat battery from starting at all.
  # Right after boot the motor bus may not report yet: give it 3 minutes to read a number
  # (11/26: a minute was not enough, the boot standby gave up with "battery unknown").
  for _ in $(seq 90); do
    volts=$(robotctl health 2>&1 | sed -n 's/.*battery *\([0-9.]*\) V.*/\1/p' | head -1)
    [ -n "$volts" ] && break; sleep 2
  done
  if ! awk -v v="$volts" 'BEGIN{exit !(v ~ /^[0-9]+(\.[0-9]+)?$/ && v + 0 > 7.0)}'; then
    echo "$(date '+%F %T') ✗ 電池 ${volts:-未知} V：不高於 7.0 V 或讀不到，不啟動。先充電。"; exit 1
  fi
  echo "$(date '+%F %T') 電池 ${volts} V；第 $attempt 次啟動 follow.app"
  python3 -u -m follow.app --live --scripted --standby --walk-m "$WALK_M" \
    --key-pipe "$KEYS" --command-pipe "$VOICE" --hot-joint left_hip_roll:80
  code=$?
  echo "$(date '+%F %T') follow.app 結束（代碼 $code）"
  # 0 = quit on purpose (q), 1 = cancelled, 130/143 = signal: do not restart.
  case $code in 0|1|130|143) exit 0 ;; esac
  sleep 5
done
echo "$(date '+%F %T') ✗ 連續當掉 3 次，不再重啟；看上面的錯誤"
