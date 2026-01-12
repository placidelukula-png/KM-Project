#!/usr/bin/env bash
set -e

python -m gunicorn app_flask_postgres:app --bind 0.0.0.0:$PORT
