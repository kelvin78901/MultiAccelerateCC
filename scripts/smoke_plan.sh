#!/usr/bin/env bash
# v0.2 smoke: one planner call on the toy repo made by smoke.sh, then (optionally) run the plan.
#   bash scripts/smoke_plan.sh            # plan only (~$0.3)
#   bash scripts/smoke_plan.sh --run      # plan + run with real workers
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${TARGET:-$HOME/macc-smoke}"
MACC="$HERE/.venv/bin/python -m macc.cli"
[ -d "$TARGET/.git" ] || { echo "run scripts/smoke.sh first"; exit 2; }
cd "$TARGET"
$MACC plan --from "Add a `stats` module to calc with mean/median/stdev, a CLI entry point (python -m calc), and tests for both. Keep stdlib only." --effort medium plan2.yaml
if [ "${1:-}" = "--run" ]; then
  $MACC plan plan2.yaml
  time $MACC run --worker claude --parallel 3 || true
  $MACC status
fi
