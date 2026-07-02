#!/usr/bin/env sh
set -e

echo "==> Creating DB if missing"
python scripts/init_db.py

echo "==> Running Alembic migrations"
alembic upgrade head

exec "$@"
