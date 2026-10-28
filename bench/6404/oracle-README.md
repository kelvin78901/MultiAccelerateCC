# Oracle for REA issue #6404

Ghidra: admit `comment`, `inline_comment`, `set_comment` and `set_inline_comment`
at any mapped address.

```
./run.sh <worktree>
```

`run.sh` builds the tree with `npm run build:cached`. It then copies
`tests/{lib,static,ghidra}.mjs` into `<worktree>/.oracle-6404-tmp/` so that bare
imports such as `@modelcontextprotocol/client` resolve from the tree's
`node_modules`, and runs each module. Afterwards it deletes the staging
directory and its temporary workspace. The last line of stdout is the JSON summary
`{"total","passed","failed","cases":[{"id","area","ok"}]}`. A one-line reason for each
failed case goes to stderr. The script exits 0 whether or not cases fail. It exits
non-zero only for a bad argument or a tree without `node_modules`.

Each module first records its planned cases. A module that crashes or cannot start
leaves its unreported cases failed. `expected-modules.json` lists every case id, so
missing modules are counted too. If the build fails, every case that needs `dist`
fails, and the source-only checks still run.

Requirements: `GHIDRA_INSTALL_DIR` (Ghidra 12.1.4), `JAVA_HOME`, a host C compiler
(`cc`, or `REA_CC`), and Node from the tree's toolchain. A run takes about
15 s on an Apple-silicon Mac after a warm build.

## Public surfaces used

| Surface | How |
| --- | --- |
| Packaged MCP server | `node <wt>/scripts/rea.mjs mcp` over stdio with real Ghidra (`REA_ANALYSIS_PROVIDER=ghidra`), driven by `@modelcontextprotocol/client`. Successful results are parsed with the tool's named contract `toolContract(name).outputSchema` (the same contract Hopper uses), and Evidence must name provider `ghidra` and the operation. |
| Target | The repository's own Ghidra verification fixture `tests/conformance/ghidra/inventory.c`, compiled with `cc -O0 -g -fno-inline` as `npm run verify:ghidra` does. Addresses are found at run time from `list_procedures`, `read_function_instructions`, `list_names` and `list_segments`; none are hard-coded. |
| Built exports | `dist/ghidra/GhidraProviderCapabilities.js` (`GHIDRA_OPERATIONS`, `CAPABILITIES`, `limitationsFor`, `windowsP0Capabilities`, `existingProjectCapabilities`), `dist/ghidra/GhidraSessionValues.js` (`GHIDRA_SESSION_CAPABILITIES`, `GHIDRA_MUTATING_OPERATIONS`), `dist/ghidra/GhidraInventoryValues.js` and `GhidraFunctionValues.js` (the lists the real Windows verifier derives from), `dist/hopper/HopperProviderCapabilities.js` (`HOPPER_OPERATIONS`), `dist/contracts/toolContracts.js` (`toolContract`). |
| Bridge Java source | `bridge/ghidra/ReaGhidraBridge.java`: the handshake `CAPABILITIES` array, and the mutating-operation declaration (any `boolean …Mutat…/…Write…(String)` method, or any `…MUTAT…`/`…WRITE…` constant). |
| Verification scripts | Source under `scripts/`, parsed statically together with every relative module each script imports. Comments are stripped first. The scripts are never run. |
| Docs | `README.md`, `docs/ghidra-provider.md`, `docs/installation.md`, `docs/native-investigation.md`, `docs/windows-ghidra-p0.md`. |
| Command | `scripts/verify-ghidra-bridge-sources.mjs`, the same command as `npm run verify:ghidra:bridge-sources`. |

Cases that check new behaviour are written so that they cannot pass on the baseline.
For example, every error-path case first requires a successful `set_comment`, so the
baseline's `capability_unavailable` rejection cannot pass for a correct input
rejection.

## Areas and cases

### operations (real Ghidra, MCP)

These cases cover issue criteria 1–4 and 6, with the same request and result shapes
as Hopper.

- `operations.regression_tools_listed`: *(regression)* the four tools are in `tools/list` with output schemas.
- `operations.session_advertises_four_available`: `binary_session` for the Ghidra session lists all four as available. Only the writes carry a mutation effect.
- `operations.set_comment_non_entry_instruction_roundtrip`: `set_comment` at a non-entry instruction returns `true`, and `comment` reads the same text back.
- `operations.set_inline_comment_non_entry_instruction_roundtrip`: the same with `set_inline_comment` / `inline_comment`.
- `operations.set_comment_data_address_roundtrip`: a regular comment at the global `rea_ghidra_inventory_global`.
- `operations.set_inline_comment_data_address_roundtrip`: an inline comment at the global `rea_ghidra_inventory_layout`.
- `operations.absent_comments_read_null`: an address with no comment reads `null` through both read operations.
- `operations.regular_and_inline_are_independent`: the regular and inline slots are separate, and clearing one keeps the other.
- `operations.regular_maps_to_pre_inline_to_eol_at_entry`: comments written with `set_comment` / `set_inline_comment` at a function entry are read back by `annotate_native_function` as `annotations.comment` (PRE) and `annotations.inline_comment` (EOL).
- `operations.annotate_entry_comments_visible_to_readers`: entry comments written by `annotate_native_function` are returned by `comment` / `inline_comment`.
- `operations.dossier_reports_regular_and_inline_kinds`: `analyze_function` reports the comments as kind `comment` (PRE) and kind `inline` (EOL) at the exact addresses.
- `operations.empty_comment_clears_regular` / `operations.empty_comment_clears_inline`: writing `""` returns `true`, and readback is then `null`.
- `operations.unmapped_set_comment_returns_false` / `operations.unmapped_set_inline_comment_returns_false`: an address past every segment returns `false` (not an error). Existing comments and `list_names` stay unchanged.
- `operations.crlf_preserved`: CRLF and mixed line endings round-trip exactly through both kinds.
- `operations.unicode_preserved`: CJK text, an emoji, a combining mark and a supplementary-plane character round-trip exactly.
- `operations.nul_rejected_without_change`: NUL text gives an `invalid_input` error, and the previous comments are kept.
- `operations.unpaired_surrogate_rejected_without_change`: lone high and low surrogates (including a trailing high one) give `invalid_input`, and the previous comments are kept.
- `operations.annotate_entry_comment_unaffected`: the entry PRE/EOL written by `annotate_native_function` survive `set_comment` / `set_inline_comment` writes and clears at body instructions.
- `operations.regression_annotate_entry_comment`: *(regression)* `annotate_native_function` entry comments, NUL rejection and readback still work.

### errors

- `errors.regression_no_session_rejected`: *(regression)* all four fail with no open target.
- `errors.invalid_address_rejected`: `not-an-address`, `0xZZ` and `""` are rejected with `invalid_input`, and the existing comment is unchanged.
- `errors.invalid_read_address_rejected`: `comment` / `inline_comment` reject malformed addresses with `invalid_input`.
- `errors.wrong_comment_type_rejected`: a numeric or `null` `comment` is rejected with `invalid_input`, and nothing changes.
- `errors.wrong_address_type_rejected`: a numeric or array `address` is rejected with `invalid_input`.
- `errors.missing_comment_rejected`: a missing `comment` or `address` is rejected with `invalid_input`.
- `errors.unknown_document_rejected`: an unknown `document` is rejected, and nothing changes.
- `errors.windows_p0_does_not_admit_writes`: `windowsP0Capabilities({available:true})` declares `set_comment` / `set_inline_comment` unavailable, mutating, and with a reason. No mutation is available.
- `errors.windows_p0_admits_reads_when_ready`: on a ready Windows host, Windows P0 admits `comment` / `inline_comment`.
- `errors.existing_project_does_not_admit_writes`: existing-project (read-only) mode refuses the writes and admits the reads.

### inventory

- `inventory.ghidra_operations_include_four`: `GHIDRA_OPERATIONS` includes each operation exactly once.
- `inventory.session_capabilities_include_four`: `GHIDRA_SESSION_CAPABILITIES` includes all four, with no duplicates.
- `inventory.mutating_operations_are_writes_only`: `GHIDRA_MUTATING_OPERATIONS` contains the two writes and not the reads, and the earlier mutating operations are still listed.
- `inventory.capability_descriptors_effects`: each operation has exactly one Ghidra `CAPABILITIES` descriptor. It is available, and `mutatesArtifact` is true only for the writes.
- `inventory.limitations_operation_specific`: `limitationsFor` gives each of the four operation-specific lines, and the descriptors carry them.
- `inventory.limitations_name_pre_and_eol`: the operation-specific limitations name the PRE and EOL mapping.
- `inventory.bridge_capabilities_match_typescript`: the Java handshake `CAPABILITIES` set equals `GHIDRA_SESSION_CAPABILITIES` and includes all four.
- `inventory.bridge_mutation_gate_covers_writes`: the Java mutating-operation declaration lists the two writes and not the reads.
- `inventory.set_comment_description_not_hopper_specific`: the `set_comment` description no longer says "the Hopper document". It names Hopper only if it also names Ghidra.
- `inventory.set_inline_comment_description_not_hopper_specific`: the same for `set_inline_comment` and "Hopper may flatten newlines".
- `inventory.docs_describe_ghidra_comment_operations`: the Ghidra docs mention both writes, and a paragraph about Ghidra (or `docs/ghidra-provider.md`) documents them.
- `inventory.ghidra_provider_doc_edit_surface`: `docs/ghidra-provider.md` no longer describes the edit surface as only "function-name/entry-comment edits", or else it mentions `set_comment`.
- `inventory.regression_hopper_declares_four`: *(regression)* Hopper still declares all four.
- `inventory.regression_tool_contract_shapes`: *(regression)* the shared contracts keep their request keys (`address`, `document`, plus `comment` for the writes) and result shapes (nullable text / boolean).
- `inventory.regression_bridge_sources_compile`: *(regression)* `verify-ghidra-bridge-sources.mjs` exits 0. This is the acceptance command `npm run verify:ghidra:bridge-sources`.

### verifiers (static enumeration completeness)

These scripts enumerate Ghidra operations or check the Ghidra inventory:

- `verify-real-ghidra.mjs`, which imports `verify-real-ghidra-inventory.mjs` (bridge rejections), `verify-real-ghidra-function.mjs`, `lib/native-value-e2e.mjs` and `lib/real-ghidra-boundaries.mjs` (MCP boundary/annotation checks).
- `verify-real-ghidra-assertions.mjs`, which checks the handshake capabilities against `GHIDRA_SESSION_CAPABILITIES`.
- `verify-real-ghidra-windows.mjs`, which builds its expected set from `GHIDRA_INVENTORY_OPERATIONS` and `GHIDRA_FUNCTION_OPERATIONS` minus `GHIDRA_MUTATING_OPERATIONS`.
- `verify-packaged-ghidra-windows.mjs`, which builds its expected set from `GHIDRA_OPERATIONS` minus `GHIDRA_MUTATING_OPERATIONS` and proves it through MCP calls.
- `lib/catalog-builders.mjs`, which builds from `GHIDRA_OPERATIONS`.

The derived checks in `verify-real-ghidra-assertions.mjs` and `lib/catalog-builders.mjs` are covered by `inventory.bridge_capabilities_match_typescript`. The cases:

- `verifiers.no_script_expects_new_operations_unavailable`: no Ghidra script still expects one of the four to be rejected (`method_unavailable`, `capability_unavailable` or "operation is not declared"). The baseline uses `set_comment` as the undeclared-mutation probe in `verify-real-ghidra-inventory.mjs`.
- `verifiers.real_ghidra_lanes_exercise_writes`: the non-Windows real-Ghidra lanes (`scripts/verify-real-ghidra*.mjs` and their imports) invoke `set_comment` and `set_inline_comment`, and do not expect them to be unavailable.
- `verifiers.real_ghidra_lanes_read_back`: those lanes also invoke `comment` and `inline_comment`.
- `verifiers.real_windows_expected_reads_admitted`: the expected set of the real Windows verifier, derived the way the script derives it, includes the reads and excludes the writes.
- `verifiers.real_windows_enumerates_every_expected_operation`: the real Windows verifier, with its imports, names every operation in its expected set and invokes `comment` / `inline_comment`. Without that, its "operation proof" fails on Windows.
- `verifiers.packaged_windows_expected_reads_admitted` / `verifiers.packaged_windows_enumerates_every_expected_operation`: the same two checks for the packaged Windows verifier.
- `verifiers.windows_lanes_do_not_require_write_success`: both Windows lanes still remove `GHIDRA_MUTATING_OPERATIONS`, and their expected sets exclude the writes.

"Invokes" means one of the call forms `fn("op", …)`, `fn(client, "op", …)`, `{ name: "op" }` or `"op", { … }`.

### edit_rules

These cases cover issue criterion 5.

- `edit_rules.writes_declared_mutating_without_snapshot_cache`: the write descriptors are mutating and have no `snapshot` cache policy.
- `edit_rules.executable_bytes_unchanged`: after comments at instruction and data addresses, `read_bytes` equals the original file bytes at `address_to_file_offset`, and the target's SHA-256 is unchanged.
- `edit_rules.comment_read_not_served_stale`: a primed `null` read is followed by a write, and the next read returns the new text. Clearing it returns `null` again, so the cached read is not replayed.
- `edit_rules.analysis_snapshot_invalidated`: `analyze_function` is primed before the write. After `set_comment` inside the function, the next dossier contains the comment.
- `edit_rules.edits_discarded_on_close`: after `close_binary` and a fresh `open_binary`, the earlier comments, including the `annotate_native_function` entry comment, read `null`.
- `edit_rules.target_file_unchanged_after_close`: the target's SHA-256 is unchanged after a write and close.

## Validation results

On the baseline checkout `baec375c3` (this repository at the task's start):
**total 60, passed 6, failed 54.**

- The 6 passing cases are all regression cases, marked *(regression)* above: `inventory.regression_hopper_declares_four`, `inventory.regression_tool_contract_shapes`, `inventory.regression_bridge_sources_compile`, `operations.regression_tools_listed`, `operations.regression_annotate_entry_comment` and `errors.regression_no_session_rejected`.
- All 54 cases that check new behaviour fail on the baseline.

To check that the new-behaviour cases can pass, I built a throwaway reference prototype in a scratch copy, outside the repository and outside this directory. It changes the Java bridge, the TypeScript inventories, the limitations, the tool descriptions, docs and verification scripts. The oracle passed **60/60** on it. With the bridge's NUL/surrogate validation and its mapped-address check removed, exactly the four matching cases failed: `nul_rejected_without_change`, `unpaired_surrogate_rejected_without_change` and the two `unmapped_*` cases.

## Known limits

- The Windows lanes are checked statically only, because no Windows host is available. The verifier cases look for literal operation names and call forms. A verifier that builds operation names dynamically (for example `"inline_" + kind`) would not be recognised.
- `inventory.bridge_mutation_gate_covers_writes` finds the Java gate by name: a method `boolean …Mutat…|…Write…(String)` or a constant `…MUTAT…|…WRITE… = …;`. That declaration must still list `set_address_name`.
- Ghidra `procedure_assembly` display of comments (the issue's open question) is not checked.
- Comment reads at unmapped addresses are not checked, because the issue leaves that behaviour unspecified.

## Operator changes after delivery (not written by the oracle author)

- 2026-10-27, `tests/lib.mjs` `invokesOperation`: added one pattern for operations invoked through a loop over an
  array literal (`for (const op of ["comment", "inline_comment"]) call(op, ...)`). Found by calibration: an earlier,
  independently reviewed #6404 implementation (on an older base) scored 59/60, the one failure being
  `verifiers.real_windows_enumerates_every_expected_operation` although that verifier does call `comment` in such a
  loop. After the patch: that implementation 60/60, the baseline still 6/60. Original file kept as `tests/lib.mjs.orig`.
