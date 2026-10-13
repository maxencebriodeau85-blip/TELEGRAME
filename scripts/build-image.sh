#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${BOT_BUILD_PYTHON:-.venv/bin/python}"
"$python_bin" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 requis pour préparer les wheels"'
"$python_bin" -m pip download --no-cache-dir --only-binary=:all: -r requirements.lock -d .wheelhouse
docker build -t telegrame-demo:local .
