# Review checklist (same for every submission)

You review anonymous submissions that each implement REA issue #6404 (text below) on the same baseline commit.
The baseline repository is the current working directory (read-only). Submission diffs are in
`SUBMISSIONS_DIR/submission_<N>.diff`. Read each diff fully and read baseline code as needed. Do not run anything.

Score every submission on each item from 1 (poor) to 5 (excellent) and give one sentence of evidence
(file:line in the diff) per score:

1. correctness: the issue's success criteria 1-6 (PRE/EOL mapping; non-entry and data round-trip; empty clears;
   unmapped returns false and changes nothing; CRLF/Unicode/NUL/surrogates as annotate_native_function; edit rules:
   bytes unchanged, snapshots invalidated, discarded on close, Windows P0 does not admit the writes; tool
   descriptions no longer Hopper-specific)
2. completeness: every place that lists, counts or verifies Ghidra operations (inventories, capabilities,
   limitations, verification scripts including the Windows real and packaged verifiers, docs) is consistent
3. architecture: AGENTS.md layering and ownership (one owner per format/provider; no provider code in
   domain/application; dependencies point inward), reuse of the existing transaction/readback/snapshot path
4. failure handling: expected failures are values (Result/tagged errors), unknown is not absent, evidence and
   partial facts preserved
5. tests: new behaviour and failure paths covered at public boundaries (docs/testing.md boundary regressions);
   existing tests not weakened or deleted
6. docs: user-facing docs and contracts accurate; historical verification claims not rewritten
7. scope discipline: no unrelated changes, no generated files committed, no new dependencies
8. risk: anything that could break other providers, Windows lanes, or existing sessions

Then: a total (sum of 1-8), a ranking of the submissions, and the single most important defect of each.

Output exactly one JSON object as the last thing you write:
{"scores": {"<N>": {"correctness": {"score": n, "evidence": "..."}, ...8 items..., "total": n,
 "top_defect": "..."}}, "ranking": ["<N>", ...]}
