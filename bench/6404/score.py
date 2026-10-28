#!/usr/bin/env python3
"""Score one bench arm for REA #6404: `score.sh <branch>` -> ~/bench/results/<branch>.json

Checks (all run in a fresh detached worktree of <branch>, never in the arm's own worktree):
  1. worktree_setup: node_modules symlink to the baseline install (~/bench/rea-deps) + `npm run build:cached`
  2. the 11 integration gates, uncached (TURBO_FORCE=true: turbo shares its cache across worktrees)
  3. npm run verify:ghidra:bridge-sources
  4. npm run verify:ghidra (real Ghidra, 20 min limit)
  5. oracle tests: ~/bench/oracle_6404/run.sh <worktree> (JSON on stdout)
  6. existing related tests: every baseline test file with "ghidra" in its path plus the listed cross-cutting ones,
     run with test:focused; regressions = files that fail here but pass on the baseline
  7. out-of-bounds: baseline paths that should not change (governance, lockfiles, generated outputs, other
     providers' sources), deleted baseline test files, removed test cases in changed baseline test files
  8. diff stats against the baseline commit
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BENCH = HOME / "bench"
REPO = HOME / "Downloads" / "rea"
BASELINE = (REPO / ".macc" / "prep" / "bench_baseline.txt").read_text().strip()
DEPS = BENCH / "rea-deps" / "node_modules"
ORACLE = BENCH / "oracle_6404" / "run.sh"
NODE_BIN = HOME / "Downloads" / "rea-toolchain" / "node-v24.22.1-darwin-arm64" / "bin"

GATES = ["npm run check:fast", "npm run format:check", "npm run knip", "npm run knip:production",
         "npm run metadata:check", "npm run docs:check", "npm run licenses:check", "npm run mcp-registry:check",
         "npm run test:python", "npm run jscpd", "npm run scan:todos"]
CROSS_CUTTING_TESTS = ["tests/boundary/cli/cliToolCoverage.test.ts",
                       "tests/composition/analysis-sessions/binarySession.test.ts",
                       "tests/boundary/mcp/officialTools.test.ts", "tests/boundary/mcp/officialArguments.test.ts",
                       "tests/boundary/mcp/toolSchemaValidity.test.ts"]
# baseline paths no #6404 change should touch
OUT_OF_BOUNDS = [r"^AGENTS\.md$", r"^CONTRIBUTING\.md$", r"^CLAUDE\.md$", r"^SECURITY\.md$", r"^LICENSE$",
                 r"^package\.json$", r"^package-lock\.json$", r"^npm-shrinkwrap\.json$", r"^\.github/",
                 r"^\.nvmrc$", r"^turbo\.json$", r"^tsconfig[^/]*\.json$", r"^vitest[^/]*\.ts$", r"^knip[^/]*\.json$",
                 r"^dist/", r"^skills/", r"^node_modules", r"^docs/public/product-catalog\.json$",
                 r"^src/(hopper|ida|jeb|cutter|lldb|gdb|frida|binja|radare|r2)/", r"^bridge/(?!ghidra/)",
                 r"^third_party/", r"^licenses/", r"^CHANGELOG\.md$"]


def env() -> dict:
    e = dict(os.environ)
    e["PATH"] = f"{NODE_BIN}:{e['PATH']}"
    e["GHIDRA_INSTALL_DIR"] = str(HOME / "tools" / "ghidra_12.1.4")
    e["JAVA_HOME"] = "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home"
    return e


def run(cmd: str, cwd: Path, logs: Path, name: str, timeout: int, extra_env: dict | None = None) -> dict:
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**env(), **(extra_env or {})}, start_new_session=True)
        code, out = p.returncode, p.stdout + p.stderr
        (logs / f"{name}.stdout").write_text(p.stdout)
    except subprocess.TimeoutExpired as e:
        code, out = None, f"{e.stdout or ''}{e.stderr or ''}\nTIMEOUT after {timeout} s"
    secs = round(time.time() - t0, 2)
    (logs / f"{name}.log").write_text(out if isinstance(out, str) else out.decode(errors="replace"))
    return {"cmd": cmd, "exit": code, "ok": code == 0, "seconds": secs}


def git(*args, cwd=REPO) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def related_tests(wt: Path) -> list[str]:
    base_files = git("ls-tree", "-r", "--name-only", BASELINE).split()
    rel = [f for f in base_files if re.search(r"\.test\.(ts|mts|mjs)$", f) and "ghidra" in f.lower()]
    rel += [f for f in CROSS_CUTTING_TESTS if f in base_files]
    return [f for f in dict.fromkeys(rel) if (wt / f).is_file()]


def parse_vitest(out: str) -> dict:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    failed = sorted(set(re.findall(r"^\s*(?:FAIL|❯|×)\s+(?:\|[^|]+\|\s+)?(\S+\.test\.\w+)", clean, re.M)))
    files = re.search(r"Test Files\s+(.*)", clean)
    tests = re.search(r"^\s*Tests\s+(.*)", clean, re.M)
    return {"failed_files": failed, "files_summary": files.group(1).strip() if files else None,
            "tests_summary": tests.group(1).strip() if tests else None}


def out_of_bounds(wt: Path) -> dict:
    changed = [ln.split("\t") for ln in git("diff", "--name-status", BASELINE, "HEAD", cwd=wt).splitlines()]
    touched = [p[-1] for p in changed]
    oob = sorted(f for f in touched if any(re.search(rx, f) for rx in OUT_OF_BOUNDS))
    deleted_tests = sorted(p[1] for p in changed if p[0] == "D" and re.search(r"\.test\.\w+$", p[1]))
    removed_cases = {}
    for st, *paths in changed:
        f = paths[-1]
        if st.startswith("M") and re.search(r"\.test\.\w+$", f):
            diff = git("diff", "-U0", BASELINE, "HEAD", "--", f, cwd=wt)
            minus = len(re.findall(r"^-\s*(?:it|test)(?:\.\w+)?\(", diff, re.M))
            plus = len(re.findall(r"^\+\s*(?:it|test)(?:\.\w+)?\(", diff, re.M))
            if minus > plus:
                removed_cases[f] = minus - plus
    return {"out_of_bounds_files": oob, "deleted_baseline_tests": deleted_tests,
            "net_removed_test_cases": removed_cases}


def diff_stats(wt: Path) -> dict:
    num = git("diff", "--numstat", BASELINE, "HEAD", cwd=wt).splitlines()
    add = rem = 0
    by_top: dict[str, int] = {}
    files = []
    for ln in num:
        a, r, f = ln.split("\t")
        a, r = (int(a) if a != "-" else 0), (int(r) if r != "-" else 0)
        add += a
        rem += r
        files.append(f)
        top = "/".join(f.split("/")[:2]) if "/" in f else f
        by_top[top] = by_top.get(top, 0) + a + r
    return {"files": len(files), "insertions": add, "deletions": rem, "by_area": by_top, "changed_files": files}


def main() -> int:
    branch = sys.argv[1]
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", branch)
    results = BENCH / "results"
    logs = results / f"{tag}.logs"
    logs.mkdir(parents=True, exist_ok=True)
    wt = BENCH / "wt" / f"score-{tag}"
    if wt.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO)
    head = git("rev-parse", branch).strip()
    git("worktree", "add", "--detach", str(wt), head)
    t0 = time.time()
    rec: dict = {"branch": branch, "head": head, "baseline": BASELINE, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (wt / "node_modules").symlink_to(DEPS)                     # worktree_setup, as macc's: deps + build output
    rec["setup_build"] = run("npm run build:cached", wt, logs, "setup_build", 900)   # knip resolves #imports in dist/
    rec["gates"] = [run(g, wt, logs, f"gate_{i:02d}", 600, {"TURBO_FORCE": "true"}) for i, g in enumerate(GATES, 1)]
    rec["gates_passed"] = sum(g["ok"] for g in rec["gates"])
    rec["bridge_sources"] = run("npm run verify:ghidra:bridge-sources", wt, logs, "bridge_sources", 600)
    rec["verify_ghidra"] = run("npm run verify:ghidra", wt, logs, "verify_ghidra", 1200)
    if ORACLE.is_file():
        o = run(f"bash {ORACLE} {wt}", wt, logs, "oracle", 1800)
        try:
            o["result"] = json.loads((logs / "oracle.stdout").read_text().strip().splitlines()[-1])
        except (ValueError, IndexError):
            o["result"] = None
        rec["oracle"] = o
    else:
        rec["oracle"] = {"skipped": f"{ORACLE} missing"}
    files = related_tests(wt)
    rt = run("npm run test:focused -- " + " ".join(files), wt, logs, "related_tests", 1800)
    rt.update(parse_vitest((logs / "related_tests.log").read_text()), files=len(files))
    base_json = results / "baseline.json"
    if branch != BASELINE and base_json.is_file():
        base_failed = set(json.loads(base_json.read_text())["related_tests"]["failed_files"])
        rt["regressions"] = sorted(set(rt["failed_files"]) - base_failed)
    rec["related_tests"] = rt
    rec.update(out_of_bounds(wt))
    rec["diff"] = diff_stats(wt)
    rec["seconds"] = round(time.time() - t0, 1)
    out = results / ("baseline.json" if branch == BASELINE else f"{tag}.json")
    out.write_text(json.dumps(rec, indent=2))
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO)
    print(json.dumps({k: rec[k] for k in ("branch", "gates_passed", "seconds")}
                     | {"bridge_sources": rec["bridge_sources"]["ok"], "verify_ghidra": rec["verify_ghidra"]["ok"],
                        "oracle": (rec["oracle"].get("result") or {}).get("passed") if "result" in rec["oracle"] else None,
                        "related_failed": len(rt["failed_files"]), "out_of_bounds": len(rec["out_of_bounds_files"]),
                        "out": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
