# Bench material (REA, 2026-10)

Two benchmarks comparing a single Claude Code session (A), background subagents (B), an agent team (C) and macc (D)
on the same REA task. Results and lessons: `docs/ROADMAP.md` ("Lessons", "Negative results").

| dir | task | baseline |
|---|---|---|
| `6404/` | REA #6404 (Ghidra comment writes) | baec375c3 |
| `4302-4304-4309/` | REA #4302 + #4304 + #4309 as one task (Info.plist declarations, privacy manifests, localizations) | ad7d4c1f4 |
| `android/` | Android wait-share step 0: build times and single-session wait share (`STEP0.md`), task C1 (`c1.md`) | 8c4deb7 |

Each directory: `task.md` (given verbatim to every arm and to `macc plan --from`), `arms/{A,B,C}.md` (task.md plus
the arm's instruction lines), `oracle-README.md` (how the hidden oracle works; the cases stay local and are not
published), `score.sh` + `score.py` (the scorer; paths point at the author's machine), `REPORT.md` (results),
`rubric.md` (blind review checklist).
