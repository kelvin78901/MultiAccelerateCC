#!/usr/bin/env bash
# Smoke test with REAL claude -p workers on a throw-away repo. Cheap by design:
# 3 tiny tasks, effort low, token budget 150k. Run from macOS, not from a sandbox.
#
#   bash scripts/smoke.sh            # uses ~/macc-smoke
#   bash scripts/smoke.sh /tmp/x     # custom location
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-$HOME/macc-smoke}"
MACC="$HERE/.venv/bin/python -m macc.cli"
# (re)create the venv if missing or built for another OS (e.g. inside a Linux sandbox)
if ! "$HERE/.venv/bin/python" -c "import macc, sys; assert sys.prefix.startswith('$HERE')" >/dev/null 2>&1; then
  rm -rf "$HERE/.venv"
  python3 -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install -q -e "$HERE[dev]"
fi
command -v claude >/dev/null || { echo "claude not on PATH"; exit 2; }

rm -rf "$TARGET"; mkdir -p "$TARGET"; cd "$TARGET"
git init -q -b main
git config user.email smoke@macc; git config user.name macc-smoke
cat > README.md <<'EOF'
# smoke
Tiny repo for exercising macc with real workers.
EOF
cat > CLAUDE.md <<'EOF'
Python 3 project. Keep files tiny. Standard library only. Never add dependencies.
EOF
git add -A && git commit -q -m init

mkdir -p .macc
# integration gate: pytest from the macc venv; "no tests collected" (rc 5) is fine on an empty repo
printf '#!/usr/bin/env bash\n"%s/.venv/bin/python" -m pytest -q; rc=$?; [ $rc -eq 0 ] || [ $rc -eq 5 ]\n' "$HERE" > .macc/gate.sh
chmod +x .macc/gate.sh

cat > plan.yaml <<'EOF'
config:
  max_parallel: 2
  token_budget: 150000
  integration_gates: ["bash __GATE__"]
defaults:
  effort: low
  max_attempts: 2
  est_tokens: 20000
tasks:
  - id: contract
    title: Define calc interface
    kind: contract
    owns: ["calc/__init__.py", "calc/interface.md"]
    done_when: ["test -f calc/interface.md"]
    est_minutes: 2
    spec: |
      Create calc/__init__.py (empty) and calc/interface.md describing two functions
      `add(a, b)` and `safe_div(a, b)` in calc/core.py; safe_div must raise ZeroDivisionError
      with message "division by zero" for b == 0. Nothing else.
  - id: impl
    title: Implement calc/core.py
    deps: [contract]
    owns: ["calc/core.py"]
    done_when: ["python3 -c 'from calc.core import add, safe_div; assert add(2,3)==5'"]
    est_minutes: 3
    spec: |
      Implement calc/core.py exactly as calc/interface.md specifies. No tests here.
  - id: tests
    title: Write tests from the interface
    deps: [contract]
    owns: ["tests/**"]
    est_minutes: 3
    spec: |
      Write tests/test_core.py using pytest against the interface in calc/interface.md
      (import from calc.core). Do not implement calc/core.py yourself. Add an empty tests/__init__.py.
EOF
sed -i.bak "s#__GATE__#$TARGET/.macc/gate.sh#" plan.yaml && rm -f plan.yaml.bak

$MACC init
$MACC plan plan.yaml
echo; echo ">>> running with real workers (claude -p, effort low)"; echo
time $MACC run --worker claude --parallel 2 || true
echo; $MACC status
echo; echo "integration tree:"; git -C .macc/worktrees/_integration ls-files
echo; echo "events:"; $MACC events --n 40
