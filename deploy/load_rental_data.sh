#!/usr/bin/env bash
# Loads the exported rental data (deploy/pm_data.json) into this server's database.
# Run from anywhere on PythonAnywhere after `git pull`, with your virtualenv active:
#   bash deploy/load_rental_data.sh
# Safe to re-run: it refuses to import twice and changes nothing in that case.
set -euo pipefail
cd "$(dirname "$0")/.."

python manage.py migrate --noinput
python manage.py pm_import_data deploy/pm_data.json --dry-run
python manage.py pm_import_data deploy/pm_data.json
