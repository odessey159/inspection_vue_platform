#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ACTION="${1:-up}"

ensure_env_file() {
  if [[ ! -f ".env" ]]; then
    if [[ ! -f ".env.example" ]]; then
      echo ".env.example not found; cannot create .env" >&2
      exit 1
    fi
    cp .env.example .env
    echo "Created .env from .env.example"
  fi
}

ensure_runtime_dirs() {
  for dir in .runtime inputs inputs/bags inputs/standards; do
    if [[ ! -d "$dir" ]]; then
      mkdir -p "$dir"
      echo "Created directory: $dir"
    fi
  done
}

show_access_info() {
  cat <<EOF

Access URLs:
  Web:      http://127.0.0.1:8700
  API:      http://127.0.0.1:8010
  Health:   http://127.0.0.1:8010/healthz

Useful commands:
  ./docker-run.sh logs
  ./docker-run.sh down

EOF
}

case "$ACTION" in
  up)
    ensure_env_file
    ensure_runtime_dirs
    echo "Starting Docker services..."
    docker compose up --build -d
    show_access_info
    ;;
  down)
    echo "Stopping Docker services..."
    docker compose down
    ;;
  logs)
    docker compose logs -f backend web
    ;;
  restart)
    ensure_env_file
    ensure_runtime_dirs
    echo "Restarting Docker services..."
    docker compose down
    docker compose up --build -d
    show_access_info
    ;;
  *)
    echo "Usage: $0 [up|down|logs|restart]" >&2
    exit 1
    ;;
esac
