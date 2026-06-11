#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-$(pwd)/pt-login-keeper}"
IMAGE="${IMAGE:-ghcr.io/duyao9992/pt-login-keeper:latest}"
PORT="${PORT:-9199}"
WEB_USER="${WEB_USER:-}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-300}"

mkdir -p "$APP_DIR/config"

cat > "$APP_DIR/docker-compose.yml" <<EOF
services:
  pt-login-keeper:
    image: ${IMAGE}
    container_name: pt-login-keeper
    restart: unless-stopped
    environment:
      CONFIG_DIR: /config
      APP_HOST: 0.0.0.0
      APP_PORT: "9199"
      CHECK_INTERVAL_SECONDS: "${CHECK_INTERVAL_SECONDS}"
      WEB_USER: "${WEB_USER}"
      WEB_PASSWORD: "${WEB_PASSWORD}"
    ports:
      - "${PORT}:9199"
    volumes:
      - ./config:/config
EOF

cd "$APP_DIR"

if docker compose version >/dev/null 2>&1; then
  docker compose pull
  docker compose up -d
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose pull
  docker-compose up -d
else
  echo "docker compose or docker-compose is required" >&2
  exit 1
fi

echo "PT Login Keeper is running: http://NAS_IP:${PORT}"
