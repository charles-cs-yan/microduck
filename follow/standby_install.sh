#!/usr/bin/env bash
# Turn the boot standby on or off: one line in the user's crontab (no sudo, no system files).
#
#   bash ~/microduck-follow/current/follow/standby_install.sh            # on: start at every boot
#   bash ~/microduck-follow/current/follow/standby_install.sh --remove   # off: delete that line
#   bash ~/microduck-follow/current/follow/standby_install.sh --show
#
# The line points at ~/microduck-follow/current, so a new deploy is picked up at the next boot.
# Undo by hand: `crontab -e` and delete the line ending in "# microduck-standby".
set -u
TAG="# microduck-standby"
LINE="@reboot sleep 20 && bash \$HOME/microduck-follow/current/follow/standby.sh $TAG"
current=$(crontab -l 2>/dev/null || true)
others=$(printf '%s\n' "$current" | grep -vF -- "$TAG" | sed '/^$/d')

case "${1:-}" in
  --show) printf '%s\n' "$current" | grep -F -- "$TAG" || echo "（沒有開機待命）" ;;
  --remove)
    if [ -n "$others" ]; then printf '%s\n' "$others" | crontab -; else crontab -r 2>/dev/null || true; fi
    echo "已移除開機待命（正在跑的 show 不受影響；要停用 standby.sh --stop）" ;;
  "")
    { [ -n "$others" ] && printf '%s\n' "$others"; printf '%s\n' "$LINE"; } | crontab -
    echo "已設定開機待命：$LINE" ;;
  *) echo "用法：standby_install.sh [--remove|--show]"; exit 2 ;;
esac
