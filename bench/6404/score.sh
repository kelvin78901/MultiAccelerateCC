#!/bin/bash
# usage: score.sh <branch|commit>   -> ~/bench/results/<branch>.json (the baseline commit -> results/baseline.json)
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <branch>" >&2; exit 2; }
exec /usr/bin/python3 -I "$HOME/bench/score.py" "$1"
