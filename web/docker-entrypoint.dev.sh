#!/bin/sh
# Ensure node_modules exists on the named volume, then start Vite for Compose HMR.
set -eu

cd /app

if [ ! -x node_modules/.bin/vite ]; then
  echo "[web] Installing npm dependencies..."
  npm ci
fi

exec npm run dev -- --host 0.0.0.0 --port 5173
