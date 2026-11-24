#!/usr/bin/env bash
# Copy follow/ to the duck and run its offline tests there. Moves nothing.
#
#   bash follow/deploy.sh [radxa@192.168.50.135]
#
# What it changes on the duck: only ~/microduck-follow/
#   releases/<timestamp>/follow/   the copied code
#   current -> releases/<timestamp> (symlink; the previous release stays for rollback)
# Undo: ssh radxa@HOST 'rm -rf ~/microduck-follow'
# Roll back: ssh radxa@HOST 'ln -sfn releases/<older> ~/microduck-follow/current'
#
# It does NOT start the app, touch robotd/mediad, install packages or need sudo.
# The offline tests talk to a fake robotd over socketpairs, never to /run/robotd.sock.
set -euo pipefail

HOST=${1:-radxa@192.168.50.135}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)

echo "→ ${HOST}:~/microduck-follow/releases/${STAMP}"
tar -C "$ROOT" --exclude='__pycache__' --exclude='*.pyc' -czf - follow \
  | ssh -o BatchMode=yes ${SSH_OPTS:-} "$HOST" "set -e
      mkdir -p ~/microduck-follow/releases/${STAMP}
      tar -C ~/microduck-follow/releases/${STAMP} -xzf -
      ln -sfn releases/${STAMP} ~/microduck-follow/current
      ls ~/microduck-follow/releases"

echo "→ offline tests on the duck"
ssh -o BatchMode=yes ${SSH_OPTS:-} "$HOST" "cd ~/microduck-follow/current && python3 --version && \
  python3 -m unittest discover -s follow/tests -t . 2>&1 | tail -3"

cat <<EOF

Deployed. Read-only next steps on the duck (safe, nothing moves):
  cd ~/microduck-follow/current && python3 -m follow.probe --seconds 30
  cd ~/microduck-follow/current && python3 -m follow.app          # dry run, keyboard only
EOF
