#!/bin/sh
set -eu

export PATH="/root/.local/bin:${PATH}"

if [ ! -x "/root/.local/share/strata/terminal/current/bin/strata" ]; then
  echo "[strata-entrypoint] Installing Strata Terminal into /root/.local/share/strata..."
  curl -fsSL https://github.com/Niko1221/Strata/releases/latest/download/install.sh | sh
fi

mkdir -p /root/.local/bin
ln -sf /root/.local/share/strata/terminal/current/bin/strata /root/.local/bin/strata

cleanup() {
  echo "[strata-entrypoint] Stopping Strata server..."
  /root/.local/bin/strata launch --stop || true
  exit 0
}
trap cleanup INT TERM

echo "[strata-entrypoint] Starting: strata launch $* --restart --yes --no-tui"
/root/.local/bin/strata launch "$@" --restart --yes --no-tui

PORT="${STRATA_INTERNAL_PORT:-8000}"
FAILS=0
while true; do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    if [ "$FAILS" -ge 6 ]; then
      echo "[strata-entrypoint] Health check failed ${FAILS} times; exiting." >&2
      /root/.local/bin/strata launch --status || true
      exit 1
    fi
  fi
  sleep 5
done

