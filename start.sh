#!/usr/bin/env bash
set -e

# Render place souvent le venv ici :
if [ -f "/opt/render/project/src/.venv/bin/activate" ]; then
  source /opt/render/project/src/.venv/bin/activate
elif [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

# Lancer gunicorn depuis l'environnement (venv)
exec gunicorn app_flask_postgres:app --bind 0.0.0.0:$PORT
