#!/usr/bin/env sh
# POSIX equivalents of the .ps1 scripts:  sh scripts/dev.sh [bootstrap|test|bench|serve]
set -e
cd "$(dirname "$0")/.."
case "${1:-serve}" in
  bootstrap) python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt ;;
  test) .venv/bin/python -m pytest tests -q -rs ;;
  bench) .venv/bin/python benchmarks/web_bench.py && .venv/bin/python benchmarks/ablations.py ;;
  serve) echo "http://127.0.0.1:8765  and  /app"; .venv/bin/python src/resolve_app.py ;;
esac
