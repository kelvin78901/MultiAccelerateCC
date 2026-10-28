# Review checklist (same for every submission)

You review anonymous submissions that each implement REA issues #4302, #4304 and #4309 together (text below) on
the same baseline commit. The baseline repository is the current working directory (read-only). Submission diffs
are in `SUBMISSIONS_DIR/submission_<N>.diff`. Read each diff fully and read baseline code as needed. Do not run
anything. You are a new reviewer: score only what the diffs show.

Score every submission on each item from 1 (poor) to 5 (excellent) and give one sentence of evidence
(file:line in the diff) per score:

1. correctness: the three issues' success criteria (#4302 per-bundle Info.plist declarations: URL types, queried
   schemes, document/UT types, activity/BGTask, services, extension point, background modes, usage descriptions,
   ATS with documented defaults, device/platform key qualifiers, fail-closed on malformed covered keys, only
   applied to Info.plist; #4304 privacy manifest discovery per bundle with digest, structure rules, TN3183/TN3184/
   TN3181, required-reason API cross-check against imports, applicability per platform, typed findings;
   #4309 .lproj inventory, declared vs observed regions, .strings (old-style, BOMs, XML/binary plist),
   .stringsdict, .xcstrings, Interface Builder key conventions, search filters, entry limits with truncation,
   read-only)
2. completeness: CLI and MCP expose the same shapes; every place that lists, counts or documents operations
   (contracts, capability inventories, CLI help, docs, README editions, skill references) is consistent
3. architecture: AGENTS.md layering and ownership (one owner per format/provider; no provider code in
   domain/application; dependencies point inward), reuse of the existing plist/bundle-resource/Mach-O paths
4. failure handling: expected failures are values (typed errors), unknown is not absent, limitations stated,
   partial facts preserved
5. tests: new behaviour and failure paths covered at public boundaries (docs/testing.md); existing tests not
   weakened or deleted
6. docs: user-facing docs and contracts accurate; historical verification claims not rewritten
7. scope discipline: no unrelated changes, no generated files committed, no new dependencies
8. risk: anything that could break existing operations (inspect_plist, bundle resources, Interface Builder
   decoding), other providers, or existing sessions

Then: a total (sum of 1-8), a ranking of the submissions, and the single most important defect of each.

Output exactly one JSON object as the last thing you write:
{"scores": {"<N>": {"correctness": {"score": n, "evidence": "..."}, ...8 items..., "total": n,
 "top_defect": "..."}}, "ranking": ["<N>", ...]}
