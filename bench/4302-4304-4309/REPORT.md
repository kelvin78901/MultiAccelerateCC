# Bench 2 report: REA #4302 + #4304 + #4309 (baseline ad7d4c1f4)

| arm | continues | finished | wall | cost | gates | Apple verifiers (macos/ib) | oracle | related regressions | out of bounds | diff | blind (/40) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A default | 0 | yes (TASK_COMPLETE) | 1879 s | $13.53 | 11/11 | pass/pass | 81/89 | 0 | package.json (+verify:apple-declarations) | 65 files +5896/-18 | 26 (3rd) |
| B subagents | 2 | yes | 2404 s | $42.72 (incl. $5.20 est.; over cap) | 11/11 | pass/pass | 81/89 | 0 | package.json (+verify:privacy-manifests) | 93 files +9764/-510 | 33 (1st) |
| C team | 0 | yes | 3751 s | $25.69 | 11/11 | pass/pass | 82/89 | 2 | none | 83 files +8891/-402 | 29 (2nd) |
| D macc v0.4.4 | n/a | no (run exit 1) | 2572 s | $19.84 (planner 3.30 + run 16.54) | 10/11 (format:check) | pass/pass | 4/89 (= baseline) | 0 | none | 9 files +2555/-0 | 17 (4th) |

Baseline: gates 11/11, oracle 4/89, related 73/75 (2 known env failures, counted as not regressions).
Oracle discovery checked by hand: A/B/C 0 "not discoverable"; D 48 (correct: D registered no new operation).
Blind reviewer: new opus session, shuffled (1=C, 2=D, 3=B, 4=A), $11.09.

## Main defect per arm
- A: single .strings/.stringsdict/.xcstrings files rejected as "unsupported binary format"; blind: duplicates bundle-anatomy ownership beside appleBundleAnatomy.ts.
- B: privacy-manifest traversal throws past max_entries and discards partial facts (blind); oracle misses URL-type role mapping and TN3183/4/1 structure rules.
- C: made inspect_plist's info_plist output required, breaking 2 existing MCP tests (BundleResourcesMcp, nativeInputContracts); localization CLI returns unprojected Result (CLI != MCP).
- D: failed in parallel code: the spike's unformatted spike.md merged unchecked and every code task's format:check then failed on it.

## D phases
planner 548 s | contract 1045 s (worker 994) | parallel code 972 s (worker 955) | integrate 0 | review 0 | fix rounds 0.
Tasks: 3 done (spike, 2 contracts), 5 failed after 2 attempts, 4 pending. Prediction 617 s / $5.38.
Failure point: spike_apple_static_readers (kind spike, done_when `test -s docs/specs/apple-bundle-declarations/spike.md`,
gate_policy per_task) merged spike.md unformatted. Then info_plist_declarations: "integration gate failed after merge:
npm run format:check" then "worker produced no changes"; privacy_manifest_rules, localization_decoders,
bundle_tools_registration: format:check on spike.md x2; apple_docs: format:check, then "changed files outside
ownership ... ['docs/specs/apple-bundle-declarations/spike.md']" (its worker fixed the file). Final gate failed; run stopped.

## v0.4.4 mechanisms in the real run
1. Derived done_when: dropped (verbatim)
   - `dropped: info_plist_declarations: 'npm run test:local -- src/domain/apple/infoPlistInterpretation.test.ts src/domain/apple/infoPlistTransportSecurity.test.ts src/domain/apple/infoPlistDeclarations.test.ts'`
   - `dropped: integrate: 'npm run verify:test-discovery'`
   - `dropped: integrate: 'npm run test:focused -- tests/boundary/cli/infoPlistDeclarationsCli.test.ts ... tests/boundary/mcp/appleBundleToolsRegistration.test.ts'`
   - moved: none. Derivation blocked nothing itself (the first drop was replaced by derived owned tests; the file
     owned by the contract task was not tracked yet, so the drop was a false positive but harmless). It did not
     cover the spike (non-code kinds are not rewritten), and it kept the whole-repo `npm run format:check` on code
     tasks as a whitelisted gate; that pairing is what blocked the run.
2. Contract error-convention check: fired nowhere (no warning in plan_from.log or plan_load.log). Plan warnings were the
   60-file ownership-gap warning and "proposed integration gate(s) not executed".
3. Exports list: present in every context pack (60 lines). Failed attempts imported listed helpers:
   bundle_tools_registration used toolContractMetadata, evidenceResultOf, ToolContract, toolContract;
   contract_bundle_tools (merged) used appleBundleSchema from appleBundleAnatomy.ts. No code task merged, so reuse in
   final code is limited to the contract.

## Deviations
- Host OOM during B session 1: all processes killed; resumed, session logged as interrupted with $5.20 estimate.
- `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` set for B (from resume) and C after B session 0 was cut by the 10-min bg wait.
- B overshot the $40 cap ($42.72): session 2 cost $27.20 against $24.48 remaining.
- D: not fixed, not rerun (as instructed). No merge, no PR.

# Addendum: D on macc v0.4.5 (last D of bench 2), 2026-10-28
Clean baseline clone ~/bench2/clones/d-macc-v045, same task.md, plan --from, plan unedited, run --parallel 6, $40.
Result: run exit 1, wall 3300 s (planner 356 + run 2938), $24.13 (planner 1.99 + run 22.14), gates 10/11
(format:check), Apple verifiers pass/pass, oracle 4/89 (48 "not discoverable": correct, nothing registered),
regressions 0, out of bounds none, diff 10 files +2781/-0 (spike + 2 contracts). 3 done, 6 failed, 3 pending.
Plan warnings (verbatim): `dropped: integrate: 'npm run verify:test-discovery'`; contract warning "contract
contract_info_plist, contract_bundle_tools does not fix return value vs throw, but impl_info_plist implements,
tests_info_plist tests/verifies"; 101-file ownership-gap warning.
Failure point: spike_bundle_reads (done_when `test -s .../spike.md`) merged an unformatted spike.md at +348 s.
impl_info_plist / impl_localizations / impl_privacy_manifests: format:check on spike.md, then ownership violation
or "no changes". tests_info_plist / tests_localizations / tests_privacy_manifests: own new tests failed both attempts
(merge_after impl_X; the tests ran before impl_X existed). Neither v0.4.5 mechanism engaged: integration_gates after
plan load was ONE chained string (`npm run check:fast && ... && npm run scan:todos`), never measured -> fast_gates
empty; `npm run format:check` != that string -> no gate_broken_on_integration.
Phases: planner 356 | contract 1170 (spike 344 -> contract_bundle_tools 812; contract_info_plist 372 in parallel) |
parallel code 1766 | integrate 0 | review 0 | fix rounds 0.
Fast-gate overhead (v0.4.5): 0 s (no fast gate selected, none ran).
Five-way blind (new reviewer, shuffled 1=A 2=C 3=B 4=D045 5=D044, $8.76): B 35, C 33, A 32, D-v0.4.5 15, D-v0.4.4 14.
Contract-phase breakdown of v0.4.4: ~/bench2/contract_phase_v044.md. Failure classification: docs/ROADMAP.md
"Negative results" (MultiAccelerateCC 2bad0d7).
