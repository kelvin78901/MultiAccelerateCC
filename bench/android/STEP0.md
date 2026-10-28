# Android wait-time experiment, step 0 (2026-10-28)
Project: /Users/kelvin/AndroidStudioProjects/MyApplication12, cloned at HEAD 8c4deb7 to ~/abench/clones/{base,baseline}.
Nothing committed to the project repository. Scripts: ~/abench/scripts; raw logs/json: ~/abench/results.

## 1. Build times (warm daemon, second of two runs)
| command | wall s | tasks |
|---|---|---|
| first build in a fresh clone (downloads Gradle 8.11.1 + deps, cold daemon) | 117.4 | 34 executed |
| clean assembleDebug | 9.0 | 35 executed |
| assembleDebug after a non-ABI edit, leaf file (AsrTextFilter.kt) | 1.2 | 4 executed |
| assembleDebug after a non-ABI edit, MainActivity.kt (4496 lines) | 3.3 | 4 executed |
| testDebugUnitTest --rerun (16 tests, no compile) | 0.7 | 1 executed |
| edit leaf file + testDebugUnitTest | 1.1 | 5 executed |
| lint: lintAnalyzeDebug --rerun lintDebug (full analysis) | 9.7 | 3 executed; exit 1 (3 pre-existing errors) |
| lint: edit leaf file + lintDebug | 11.1 | 10 executed; exit 1 |
| lint, first time in a project dir | 31.8 | run 1 only |
| emulator boot (Medium_Phone_API_36.1, headless) | 30 | - |
| connectedDebugAndroidTest (1 test, incl. install) | 33 | run 2; run 1 = 65 |

## 2. Wait share, baseline task (History export, default claude -p, $15 cap, BASH_MAX_TIMEOUT_MS=900000)
Model claude-opus-5-5, 1 session, 23 turns, $1.03, wall 125 s (transcript 122 s), TASK_COMPLETE; verified by me:
assembleDebug + testDebugUnitTest pass (14 new tests), lint shows only the 3 pre-existing errors. Diff 5 files +240.
Bash: 11 calls, 36.7 s = 30.1% of wall. Gradle: 2 calls, 31.6 s = 25.9% of wall (30.0 s for the first
`assembleDebug testDebugUnitTest lintDebug` in a fresh clone, 1.6 s for the re-check). Remaining ~85 s = model time.
The task took 2 minutes, not 30: the share is from a small task.

## 3. Candidates (all surfaces pure Kotlin unless noted; minimal check = filtered unit test after an edit)
Per-surface `./gradlew testDebugUnitTest --tests <class>` after an edit: 0.8-1.1 s (non-ABI and ABI edits alike).
See ~/abench/results/surface_times.json.

## 2b. Wait share, bigger task (candidate C1 as one session, default claude -p, $15 cap)
Task: ~/abench/tasks/c1.md (AsrTextFilter hallucination/repetition, GlossaryFileParser, SpmParser.parseChecked).
claude-opus-5-5, 1 session, 27 turns, $1.38, wall 262 s (transcript 258 s), TASK_COMPLETE; verified by me:
assembleDebug + testDebugUnitTest pass (63 tests in 4 classes: 1 + 28 + 15 + 19), lint only the 3 pre-existing
errors. Diff 7 files +812/-63, only the allowed files.
Bash: 12 calls, 45.0 s = 17.5% of wall. Gradle: 3 calls, 43.8 s = 17.0% (5.4 s test after writing the parser test,
29.7 s first assemble+test+lint in the fresh clone, 8.7 s final re-check). Model time ~213 s = 83%.
My 30-60 min estimate for C1 was off by ~10x: one session did all three surfaces in 4.4 min.
