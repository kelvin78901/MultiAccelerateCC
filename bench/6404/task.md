# Task: REA issue #6404

Repository: REA (reverse-engineer-anything), checked out at the current working directory on its own branch.
Implement the GitHub issue below in this repository.

## Issue #6404 (verbatim)

[Feature] Ghidra: admit comment / inline_comment / set_comment / set_inline_comment at any mapped address

### Before submitting

- [X] I searched the open and closed issues and pull requests for a related proposal.
- [X] I understand the [contribution scope](https://github.com/morluto/rea/blob/main/CONTRIBUTING.md#contribution-scope) must be reviewed before integration; open questions are welcome here.
- [X] This request does not include credentials, private samples, or sensitive target data.

### Area

Native analysis or decompilation

### Current limitation

In a Ghidra session an agent can only write a comment at the exact entry of a function. Examined at REA `main` e83e3ceadd97770a50b8afa97f4077916e4d2b81 (source reading):

- The Ghidra adapter's admitted operations (`src/ghidra/GhidraInventoryValues.ts`, `GHIDRA_INVENTORY_OPERATIONS`) include `set_address_name` and `set_addresses_names` but none of `comment`, `inline_comment`, `set_comment` or `set_inline_comment`. Hopper declares all four (`src/hopper/HopperProviderCapabilities.ts`).
- Ghidra's only comment write is `annotate_native_function`, where "Regular comments map to PRE and inline comments to EOL at the exact function entry" (`src/ghidra/GhidraProviderCapabilities.ts`). `docs/ghidra-provider.md` describes the edit surface as "atomic function-name/entry-comment edits".
- The `set_comment` tool contract tells agents to "use comments to record evidence IDs or reasoning" (`src/contracts/officialToolContracts.ts`), which a Ghidra user cannot do at a specific instruction, jump-table entry or global.

#1634 admitted the naming operations for Ghidra and explicitly deferred comments.

### Use cases

- Recording an Evidence ID or a decoded constant next to the instruction that uses it, in a function body rather than at its entry.
- Annotating a global (`DAT_...`) or a data table with what it holds, so later `procedure_pseudo_code` readers and other agents see the note.
- Reading back comments that Ghidra's own analyzers or imported PDB/DWARF data placed at non-entry addresses.

### Desired outcome and success criteria

`comment`, `inline_comment`, `set_comment` and `set_inline_comment` work with the Ghidra provider with the same request and result shapes as Hopper:

1. A regular comment maps to Ghidra's PRE comment and an inline comment to EOL, consistent with `annotate_native_function`.
2. Setting a comment at a non-entry instruction and at a data address returns `true`, and `comment` / `inline_comment` read the same text back.
3. An empty comment clears the comment. An unmapped address returns `false` and leaves the session unchanged.
4. CRLF, Unicode and rejection of NUL / unpaired surrogates behave as `annotate_native_function` already guarantees.
5. The write operations follow the existing Ghidra edit rules: executable bytes stay unchanged, analysis snapshots are invalidated, edits are discarded on close, and Windows P0 does not admit them.
6. The `set_comment` / `set_inline_comment` tool descriptions stop being Hopper-specific (today `set_comment` says it mutates "the Hopper document" and `set_inline_comment` warns that "Hopper may flatten newlines").

Open check for the implementer: whether Ghidra `procedure_assembly` output should show these comments, and how that interacts with existing output budgets.

### Expected interaction with targets

- Write or change files/configuration

### Target and scope details (optional)

The write is to REA's ephemeral Ghidra session database only, the same authority `set_address_name` already has; the target file is not modified and no Ghidra project is saved. I selected "Write or change files/configuration" because the session database is a file REA owns; if maintainers classify session metadata edits differently, this selection should follow that classification. Non-goals: PLATE/POST/REPEATABLE comment types, bookmarks, and persisting comments across sessions (#6405).

### Verification available so far

I can suggest a sample or manual check

### Verification details (optional)

Proposed check on the existing Ghidra verification fixture: set a regular comment at an instruction inside a function body and an inline comment at a data address, read both back, clear one with an empty string, try an unmapped address, then confirm that `annotate_native_function`'s entry comment is unaffected. Not run: no local Ghidra was available for this report.

### Alternatives considered

- `annotate_native_function`: function entry only, and every call returns a full dossier (#1634 measured about 148k characters for one rename).
- Opening the target in Hopper for comments: needs a second provider and session, and the comments do not appear in the Ghidra session's pseudocode or names.

### Possible direction

Reuse the transaction, readback and snapshot-invalidation path that `annotate_native_function` and `set_address_name` already use in `bridge/ghidra/ReaGhidraBridge.java`. Recent Ghidra releases expose comment types through a `CommentType` enum; the implementer should check which form the supported Ghidra versions require.

### References

- Related: #1634 (closed; deferred comments), #5339 (open; another Ghidra session edit).


## Acceptance criteria

The work is accepted when all of the following hold on the final tree:

1. The four operations `comment`, `inline_comment`, `set_comment` and `set_inline_comment` work with the Ghidra
   provider with the same request and result shapes as Hopper, meeting success criteria 1-6 of the issue.
2. These commands all exit 0:
   - `npm run verify:ghidra:bridge-sources`
   - `npm run verify:ghidra` (real Ghidra)
   - the 11 project checks: `npm run check:fast`, `npm run format:check`, `npm run knip`,
     `npm run knip:production`, `npm run metadata:check`, `npm run docs:check`, `npm run licenses:check`,
     `npm run mcp-registry:check`, `npm run test:python`, `npm run jscpd`, `npm run scan:todos`
3. Documentation, the real-Ghidra verification scripts, and the Windows verification lanes (both the real and the
   packaged Windows verifier) are consistent with the new operations.
4. Do not run the full test suite (`npm test`, `npm run test:run`, `npm run test:with-build`) or
   `npm run check:pr`. Run the specific test files you add or change (`npm run test:local -- <file>` for
   source tests, `npm run test:focused -- <file>...` for tests/boundary and tests/acceptance).

## Environment

- Node 24 is first on PATH; `node_modules` is installed. Run plain `npm run ...`.
- Ghidra 12.1.4 is installed: `GHIDRA_INSTALL_DIR` and `JAVA_HOME` (Temurin 21) are exported. Do not change them.
- macOS host. No Windows host is available.
- No network access is needed; do not add dependencies.
