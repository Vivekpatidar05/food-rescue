#!/bin/bash
# deploy.sh — one-command production deploy for FoodRescue.
#
# Run ON THE DROPLET:  bash /opt/food-rescue/scripts/deploy.sh
#
# Pulls the latest master, reinstalls Python deps only if requirements.txt
# changed, restarts the API, health-checks it, and AUTO-ROLLS-BACK to the
# previous commit if the new version fails to come up. Frontend files need no
# post-pull rewrite any more — the app now auto-detects its API origin.
#
# The whole body runs inside main() so that `git pull` updating this very file
# mid-run cannot change what is already executing.
set -euo pipefail

APP_DIR=/opt/food-rescue
SERVICE=foodrescue
HEALTH=http://127.0.0.1:5000/health

main() {
  cd "$APP_DIR"

  local prev req_before req_after
  prev=$(git rev-parse HEAD)
  req_before=$(md5sum backend/requirements.txt | awk '{print $1}')

  # Drop any legacy local edits (e.g. the old same-origin sed) so pull is clean.
  git checkout -- frontend/ 2>/dev/null || true

  echo "==> git pull"
  git pull --ff-only

  echo "==> now at: $(git log -1 --oneline)"

  req_after=$(md5sum backend/requirements.txt | awk '{print $1}')
  if [ "$req_before" != "$req_after" ]; then
    echo "==> requirements.txt changed -> pip install"
    backend/.venv/bin/pip install -r backend/requirements.txt -q
  else
    echo "==> deps unchanged -> skip pip"
  fi

  echo "==> restart $SERVICE"
  systemctl restart "$SERVICE"

  echo "==> health check"
  local ok=0 i
  for i in $(seq 1 15); do
    if curl -sf "$HEALTH" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done

  if [ "$ok" != 1 ]; then
    echo "!! health check FAILED — rolling back to $prev"
    git reset --hard "$prev"
    if [ "$req_before" != "$req_after" ]; then
      backend/.venv/bin/pip install -r backend/requirements.txt -q
    fi
    systemctl restart "$SERVICE"
    echo "!! rolled back. Deploy aborted."
    exit 1
  fi

  echo "==> deployed OK: $(curl -s "$HEALTH")"
}

main "$@"
