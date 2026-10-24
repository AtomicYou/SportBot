#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
if [[ ${EUID} -ne 0 ]]; then echo 'Run this script with sudo.' >&2; exit 1; fi
if [[ ! ${1:-} =~ ^[1-9][0-9]*$ ]]; then echo 'Usage: sudo bash deploy/ubuntu/start.sh NUMERIC_TELEGRAM_ID' >&2; exit 1; fi
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    echo 'Created .env. Fill BOT_TOKEN and MINIAPP_URL, then run this script again.'
    exit 1
fi
python3 deploy/ubuntu/check-config.py
docker compose config --quiet
docker info >/dev/null
# Use Compose interpolation, not shell evaluation of a file containing secrets.
data_dir=$(docker compose config --format json | python3 -c 'import json,sys; c=json.load(sys.stdin); print(next(v["source"] for v in c["services"]["bot"]["volumes"] if v["target"]=="/app/data"))')
if [[ "$data_dir" != /* || "$data_dir" == / || -L "$data_dir" ]]; then echo 'Unsafe data directory.' >&2; exit 1; fi
if [[ ! -d "$data_dir" ]]; then install -d -o 10001 -g 10001 -m 700 "$data_dir"; fi
if [[ $(stat -c '%u' "$data_dir") != 10001 ]]; then
    echo 'Data directory must belong to UID 10001. Review imported file permissions; ownership was not changed automatically.' >&2
    exit 1
fi
systemctl enable --now docker
docker compose build
docker compose run --rm --no-deps bot python setup_owner.py "$1"
docker compose up -d --wait --wait-timeout 180
echo 'SportBot started. Status: sudo docker compose ps; logs: sudo docker compose logs -f --tail=100 bot'
