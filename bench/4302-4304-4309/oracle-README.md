# REA oracle for issues #4302, #4304 and #4309

Hidden acceptance suite for the REA benchmark tasks:

- **#4302**: Info.plist interpretation in `inspect_plist`.
- **#4304**: privacy manifests and the required-reason API cross-check.
- **#4309**: `.lproj` inventory and string-table decoding.

```sh
/Users/kelvin/bench2/oracle/run.sh <worktree>
```

`run.sh` builds the tree with the repository's own `npm run build:cached` (it uses the `node_modules`
already linked in the tree) and builds fixtures in a fresh temporary directory. It then runs every case
against the built CLI (`scripts/rea.mjs`) and one stdio MCP server (`scripts/rea.mjs mcp`). The last
stdout line is the JSON summary:

```json
{"total": 89, "passed": N, "failed": N, "cases": [{"id": "...", "area": "4302|4304|4309|surface", "ok": true}]}
```

Above that line it prints one `PASS`/`FAIL` line per case, with the failure reason. `run.sh` exits 0
whether cases pass or fail. It exits non-zero only when the oracle itself cannot run: the worktree is
missing, `node_modules` is missing, `scripts/rea.mjs` is missing, or the runner crashes. A build failure is
logged to stderr, and the cases then run against whatever the tree provides, so they fail. The oracle copies
nothing into the worktree and writes nothing there except the build output.

Optional environment variables:

| Variable | Effect |
| --- | --- |
| `ORACLE_SKIP_BUILD=1` | Skip `npm run build:cached` |
| `ORACLE_ONLY=<regex>` | Run only the case IDs that match |
| `ORACLE_VERBOSE=1` | Print discovery results and progress to stderr |
| `ORACLE_CONCURRENCY` | Number of parallel CLI processes (default 4) |
| `ORACLE_CASE_TIMEOUT_MS` | Timeout per case (default 600000) |
| `ORACLE_KEEP_TMP=1` | Keep the fixture directory |
| `ORACLE_PASS_ENV=A,B` | Forward the named variables to the REA child processes; only the self-checks below use it |

A run takes about 10 s on the baseline once the build is cached. On a changed tree, the TypeScript compile
adds to that.

## Layout

| File | Contents |
| --- | --- |
| `run.sh` | Build, then run `oracle.mjs` |
| `oracle.mjs` | Entry point: fixtures, harness, discovery, cases, summary |
| `lib/harness.mjs` | CLI runner, MCP client from the tree's own `@modelcontextprotocol/client`, Ajv from the tree's `ajv`, schema-driven discovery, session handling |
| `lib/fixtures.mjs` | Fixtures built at run time with `plutil`, `xcrun clang`, `xcrun xcstringstool`, `xcrun ibtool`, `zip`, byte writes for UTF-8/UTF-16 BOM variants, and symlinks for versioned frameworks |
| `lib/json.mjs` | Structure-agnostic search over results; see "How results are read" |
| `cases/p4302.mjs`, `cases/m4304.mjs`, `cases/l4309.mjs`, `cases/surface.mjs` | The cases |

## Public surfaces used

- **CLI:** `node <worktree>/scripts/rea.mjs <command> <path> [options] --format json`. Every case that
  checks a behaviour reads the CLI result. Typed CLI errors are JSON objects with string `code` and
  `category` or `message` fields, and a non-zero exit.
- **MCP:** `tools/list`, including pagination: tool names, descriptions, input schemas and advertised
  output schemas. `tools/call` serves the CLI/MCP parity, contract and typed-error cases. Session-bound tools
  get a session first: `open_bundle_resources` on the containing `.app`, or `open_binary` as a fallback. The
  existing `inspect_plist` is called this way.
- **Tool contracts:**
  - Ajv (2020-12) validates the MCP `structuredContent` and the CLI JSON against the advertised
    `outputSchema`.
  - `toolContract(name).outputSchema.safeParse` comes from the built `dist/contracts/toolContracts.js`, the
    same export the repository's own acceptance tests use.
  - The advertised output schema must declare every top-level key of the result.
- **Documentation:**
  - Every `docs/**/*.md` file, excluding `docs/readme/` and `docs/releases/`.
  - `README.md` and every `docs/readme/README_*.md`.
- **Stock apps:** `/System/Applications/Stickies.app` (NSServices, the absent LSHandlerRank,
  `ServicesMenu.strings`, `.loctable`) and `/System/Applications/Calculator.app` (`~ipad`/`~iphone`
  folding, absent covered keys).

No case imports private modules or depends on a file name an implementer would choose. The existing
`docs/`, `README.md` and `docs/readme/` editions are read as collections.

## How operations are discovered (#4304 and #4309)

New tool and command names are never hard-coded. `Harness.discover(topic)` scores every MCP tool from
`tools/list` and every CLI command from `rea --llms-full --format json`, which gives each command's name,
description and args/options JSON Schema.

- **Name hit** (+10). Privacy: `/privacy|xcprivacy|required[_-]?reason/`. Localization:
  `/locali[sz]|string[_-]?tables?|lproj|xcstrings|stringsdict/`.
- **Mode hit** (+8). An input property whose `enum`, `const`, `anyOf` or `oneOf` value names the topic (for
  example `view: "privacy_manifests"` or `content: "localizations"`), or a boolean property whose name names
  the topic. Root-level `oneOf`/`anyOf` branches are merged. The mode value is sent on every call: the MCP
  argument, or `--<option> <value>` on the CLI.
- **Description vocabulary** (+2 per hit) and **schema vocabulary** (+1 per hit). Privacy: `xcprivacy`,
  `PrivacyInfo`, privacy manifest, required reason, `NSPrivacy`. Localization: `xcstrings`, `stringsdict`,
  `lproj`, string table, localization, `.strings`.
- **Qualifying.** A tool qualifies with a name hit and a score of at least 10, a mode hit and a score of at
  least 9, or at least 3 description hits and a score of at least 8. The best score wins.
- **Exclusions.** Non-Apple families (Android/APK, Harmony, JEB, adb, web, Electron, JavaScript, managed) are
  excluded. On the baseline, `decode_android_resources --string-tables-only` was a false positive.
- **CLI pairing.** The CLI command is the kebab-case MCP name when that command exists. When the MCP side is
  a mode, the CLI command must carry the matching mode option. Otherwise the CLI command is scored
  independently.
- **Path and detail arguments.** The path parameter is picked from the input schema: `path`, `target`,
  `*_path` and similar names. A `detail`/`view` enum that offers `complete`/`full` is set to it, so summary
  defaults never hide the Evidence.
- **Search and limit options (#4309).** These are found by name: key or value filters, a generic
  query/search/filter, and `max`/`limit` with `entr`/`row`/`result`.

A case fails with "operation not discoverable" when the operation cannot be found. On the baseline, both
topics discover nothing.

## How results are read

Implementers choose their own field names, so cases locate facts by the values the fixtures define. They
then read the surrounding context:

- `occurrences(root, pred)` finds every scalar value or object key that matches.
- `ctx(occ)` collects the context of an occurrence: the keys on its path, the scalars of every enclosing
  object, and those objects' small object children.
- `levels`, `valueStatus` and `hitStatus` classify status tokens from the innermost level outward, using
  short strings only. So "documented", "outside_documented_values", `documented: false`, "listed",
  "not_listed", "declared", "missing", "category_ambiguous" and "not_established" are recognized whether
  they appear as strings, keys or booleans.
- `deepestWith(root, preds)` finds the smallest subtree that holds several co-located facts, for example a
  document type with its name, content type, rank, role and class.
- For #4302, the interpreted section is every `normalized_result` key the baseline does not have. The
  baseline keys are `format`, `value`, `bundle`, `source_path`, `resource_root`, `provenance` and
  `limitations`. Raw echoes in `value` therefore never satisfy a new-behaviour case.
- For #4304, a finding is a record with a verdict-like field (`verdict`, `validity`, `valid`, `result`,
  `outcome`, `status`, `severity`, `assessment` or `state`) that cites an `NSPrivacy…` key path, or an Apple
  rule together with "property list". The innermost such record is kept. Import-hit records are excluded.

## What each area checks

### 4302: `inspect_plist` Info.plist section

Fixture `Demo.app` (macOS layout, `/bin/ls` as its executable) declares:

- two URL types, one without a role;
- `LSApplicationQueriesSchemes`;
- a document type with rank `Owner` and role `Viewer`;
- an identifier in both the exported and the imported type declarations;
- `NSUserActivityTypes`, `NSServices` and `BGTaskSchedulerPermittedIdentifiers`;
- the usage descriptions `NSCameraUsageDescription` and the unlisted `NSTeleporterUsageDescription`;
- `UIBackgroundModes = [audio, teleport]` and a `~ipad` key;
- ATS with `NSAllowsArbitraryLoads` and `NSAllowsArbitraryLoadsInWebContent` both true, an empty
  `example.com` exception domain, and a pinned domain with one CA and one leaf `SPKI-SHA256-BASE64` value.

It also has `Share.appex` (`com.apple.share-services`) and `Tunnel.appex`
(`com.apple.networkextension.packet-tunnel`). The other fixtures:

- `Variants.app`: `UIBackgroundModes~ipad = [fetch, warp]` and `NSUserActivityTypes-amiga~toaster`.
- `Malformed.app`: `CFBundleURLSchemes` as a string.
- `Minimal.app`: no covered keys.
- Loose plists: `other.plist` (a copy of Demo's Info.plist), a launchd plist, an `Info.plist` with an array
  root, and a lowercase `info.plist`.

The cases check entry points, capabilities, defaults, value status, fail-closed handling of malformed keys,
suffix folding, ATS globals, domains and pins, extension points per bundle, applicability, coverage,
not-declared keys, limitations, the description and docs, and the unchanged `value`/`bundle`.

### 4304: privacy manifests

The fixtures are iOS-family `.app` directories built with `xcrun --sdk iphoneos clang -target
arm64-apple-ios17.0`:

- the executable imports `_stat`, `_statfs` and `_mach_absolute_time`;
- `Frameworks/Lib.framework/Lib` is a dylib that imports `_getattrlist`.

Variants:

- `Fixture`: the manifest from the issue.
- `Moved`: the manifest moved to `Resources/`.
- `Fixed`: `PhotosorVideos`.
- `LibDeclared`: Lib.framework declares FileTimestamp `C617.1`.
- `Defaults`: declares UserDefaults `CA92.1`.
- `Garbage`: manifest bytes that are not a plist.
- `Rules`: an accessed-API dictionary with a third key, a collected-data dictionary without Tracking, and an
  empty reasons array.
- `Domains`: a domain with a path and query, and one with a trailing slash.
- `Unknown`: an unknown category, and `NSPrivacyTracking` as a string.
- `Nested`: an app extension with its own manifest, and a loose `Frameworks/libloose.dylib` covered by the
  app's FileTimestamp-only manifest.
- `Fixture.ipa`: a zip of `Payload/Fixture.app`.

macOS fixtures:

- `Mac.app` (arm64 macOS 14): a `Contents/Resources` manifest, a versioned `MacLib.framework` with its
  manifest in `Versions/A/Resources`, and `Helper.xpc`.
- `Mac86.app`: x86_64 macOS 11, so imports carry `$INODE64`.
- `Catalyst.app`: `arm64-apple-ios17.0-macabi`.

Manifest digests are checked against the real SHA-256 of each fixture file.

### 4309: localizations

`Fixture.app` is built as the issue describes:

- `xcstringstool compile --serialization-format binary` of `Localizable.xcstrings` and
  `InfoPlist.xcstrings`;
- `ibtool --compile` of `Base.lproj/Main.xib`, a `NSMenuItem` with ID `gfa-oA-9cr`;
- `ibtool --export-strings-file` for `fr.lproj/Main.strings` (UTF-16 LE with IB comments).

It adds:

- `Legacy.strings` in UTF-8 with a comment and an escaped quote (`en`), UTF-8 with BOM (`en_GB`), UTF-16 BE
  with BOM and a comment (`fr`), and UTF-16 LE without BOM (`de`);
- an XML-plist `es.lproj/Localizable.strings`;
- a comment-less `es.lproj/Main.strings`;
- a `.stringsdict` with device and width rules, and one plural without `other`;
- a nested `Widget.appex` with development region `fr`;
- `CFBundleLocalizations` that includes the unobserved `pt-PT`.

`Broken.app` has a bad `.strings`, a duplicate key, a same-name `.xcstrings`/`.strings` pair and a
`.loctable`. Single-file targets: `Localizable.xcstrings`, `Device.xcstrings` (device variation, version
1.1), a malformed `.xcstrings`, the UTF-16 `Main.strings` and the fr `.stringsdict`.

### Surface (per new or changed operation)

- **CLI/MCP parity:** the result shapes are equal (key-path sets).
- **Contracts:** Ajv accepts the result against the advertised `outputSchema`, the built `toolContract`
  parses it, and the schema declares the result keys.
- **Typed errors:** a missing target gives a typed value on the CLI and MCP, with no stack trace.
- **Docs:** a topic guide documents the tool with its file types, and some page pairs `tool_name` with
  `rea cli-name`, which is the CLI/MCP reference convention in `docs/native-investigation.md`.
- **README editions:** the English README names the operation, or links a guide that documents it, and
  every translated edition keeps the same names and links.
- **Existing behaviour:** `inspect_plist` CLI/MCP agreement, `inspect_plist` typed errors, and the
  `decode_interface_builder` `NSKey`.

## Case list

`kind` is `existing` for behaviour the baseline already has, which must pass, and `new` for behaviour the
issues add, which must fail on the baseline.

| ID | Area | Kind | Checks |
| --- | --- | --- | --- |
| p_url_types_scheme_role_meaning | 4302 | new | Scheme `demo` with source path CFBundleURLTypes/CFBundleURLSchemes, role Editor, meaning, URL name |
| p_url_role_absent_defaults_none | 4302 | new | URL type without a role is reported as `None`, marked defaulted |
| p_queried_schemes_activity_bgtask | 4302 | new | `othertool` (LSApplicationQueriesSchemes with meaning), activity type, BGTask identifier |
| p_document_types_rank_role | 4302 | new | Document type name, content type, class, rank Owner and role Viewer marked documented |
| p_type_declarations_precedence | 4302 | new | Exported/imported identifiers, conformances and tags; the exported-takes-precedence note |
| p_services_fixture_subkeys | 4302 | new | NSServices menu item, message, port, send/return types, key equivalent |
| p_background_modes_documented_vs_outside | 4302 | new | `audio` documented, `teleport` outside the documented values and kept |
| p_usage_descriptions_resource_and_unlisted | 4302 | new | Camera resource with the string as written; an unlisted usage key reported as such |
| p_ats_modern_arbitrary_loads_no | 4302 | new | iOS 10+ and macOS 10.12+: NO, with the ignoring rule |
| p_ats_legacy_arbitrary_loads_yes | 4302 | new | iOS 9.0 and macOS 10.11: YES, with a rule |
| p_ats_exception_domain_defaults | 4302 | new | `example.com`: global keys do not apply; forward secrecy YES and TLSv1.2 marked defaulted; subdomains |
| p_ats_pinned_domain_identities | 4302 | new | Pinned CA and leaf SPKI values, subdomain inclusion, the match-each-category rule |
| p_extension_point_listed | 4302 | new | Share.appex `com.apple.share-services` listed on the key page |
| p_extension_point_not_listed_not_invalid | 4302 | new | Packet tunnel not listed and not invalid; the app's own result has neither extension point |
| p_stickies_services_and_rank_default | 4302 | new | Stickies `makeStickyFromTextService` and send types; NSExecutable raw and undocumented; LSHandlerRank Default, defaulted |
| p_calculator_device_suffix_folding | 4302 | new | Calculator `~ipad`/`~iphone` grouped under the base key with qualifiers and source keys |
| p_unrecognized_qualifier_not_folded | 4302 | new | `NSUserActivityTypes-amiga~toaster` reported as an unrecognized qualifier |
| p_variant_interpreted_per_variant | 4302 | new | `UIBackgroundModes~ipad` values interpreted per variant (`warp` outside, `fetch` documented) |
| p_malformed_covered_key_fails_closed | 4302 | new | String CFBundleURLSchemes reported as malformed with its raw value; exit 0; the rest of the section is present |
| p_non_info_plist_not_applied | 4302 | new | `other.plist`: not applied, with an Info.plist reason, and no interpretation |
| p_not_applied_launchd_arrayroot_lowercase | 4302 | new | Launchd plist, array-root Info.plist and lowercase `info.plist` are not applied, each with a reason |
| p_absent_covered_keys_not_declared | 4302 | new | Calculator's absent covered keys are reported as not declared |
| p_coverage_names_families | 4302 | new | An empty Info.plist names every key family and reports each one as not declared |
| p_limitations_declarations_ats_localization | 4302 | new | Declaration, URL Loading System, older-SDK and localized-override limitations |
| p_value_bundle_unchanged | 4302 | existing | `value` equals the decoded plist (Demo and other.plist); `bundle` and `format` unchanged |
| p_description_and_docs_name_section | 4302 | new | The tool description and a docs page name the section key and at least 9 of the 11 families |
| m_root_manifest_found_with_digest | 4304 | new | iOS root manifest found with its real SHA-256 |
| m_framework_manifest_absent | 4304 | new | Lib.framework's expected root location is reported as absent |
| m_moved_manifest_unattributed | 4304 | new | A manifest moved to Resources/ leaves the app's manifest absent and is listed as unattributed |
| m_bundle_kind_and_platform | 4304 | new | App and framework kinds with platform iOS from the Mach-O build metadata |
| m_three_invalid_findings | 4304 | new | Tracking/TN3181, PhotosOrVideos outside the documented set, 85F4.1 invalid for FileTimestamp; exactly 3 |
| m_case_sensitive_data_type_clears_one | 4304 | new | `PhotosorVideos` clears only that finding (2 remain) |
| m_structure_rules_tn3183_tn3184_tn3181 | 4304 | new | Third key, missing Tracking and empty reasons each produce a finding citing TN3183, TN3184 and TN3181 |
| m_cross_check_fixture_executable | 4304 | new | mach_absolute_time declared; stat declared and linked to the 85F4.1 finding; statfs missing (DiskSpace) |
| m_cross_check_framework_missing | 4304 | new | Lib getattrlist is missing, with both candidate categories |
| m_cross_check_framework_category_ambiguous | 4304 | new | With a FileTimestamp-only Lib manifest, getattrlist is category_ambiguous |
| m_declared_category_without_import_not_established | 4304 | new | UserDefaults: declared, no import observed; not_established limitation; no error |
| m_applicability_required_on_ios | 4304 | new | iOS hits carry applicability `required` |
| m_macos_not_required_with_platform_limitation | 4304 | new | macOS hits are `not_required`, with a limitation naming iOS, iPadOS, tvOS, visionOS and watchOS |
| m_macos_manifest_locations | 4304 | new | Contents/Resources, Versions/A/Resources (framework) and XPC manifests found with digests; validated |
| m_x86_64_inode64_same_mapping | 4304 | new | x86_64 `$INODE64` imports map and resolve the same way as arm64 |
| m_non_plist_manifest_single_finding | 4304 | new | Undecodable manifest: an invalid-property-list finding, no per-key verdicts, other bundles reported |
| m_value_set_source_and_import_limitations | 4304 | new | Value-set source and date; import-coverage and static-linking limitations (iOS and macOS) |
| m_mac_catalyst_applicability_unknown | 4304 | new | Mac Catalyst hits carry applicability `unknown` |
| m_nested_appex_and_loose_dylib | 4304 | new | Appex kind and root manifest, appex hits checked against the appex manifest, loose dylib covered by the app |
| m_ipa_input | 4304 | new | IPA input: statfs missing; root manifest digest reported |
| m_tracking_domain_shape | 4304 | new | Domains with a path/query or a trailing slash are invalid; a plain domain is not |
| m_unknown_category_and_top_level_type | 4304 | new | Unknown category is not in the documented set; string NSPrivacyTracking gives a type finding |
| l_inventory_region_declared_observed | 4309 | new | Development region en, raw CFBundleLocalizations (pt-PT), observed lproj directories including Base |
| l_tables_per_localization | 4309 | new | Table names Localizable (en, fr), InfoPlist (fr), Main (fr) |
| l_binary_plist_entry | 4309 | new | fr greeting = Bonjour, binary plist, from Localizable.strings |
| l_stringsdict_plural_variants | 4309 | new | `%#@value@`, variable `value`, NSStringPluralRuleType, `lld`, one and other |
| l_infoplist_base_value | 4309 | new | fr CFBundleDisplayName = Démo, with base Info.plist value Demo |
| l_interface_builder_comment_keys | 4309 | new | gfa-oA-9cr.title: UTF-16 LE, class NSMenuItem, object_id, property |
| l_interface_builder_nib_join_convention | 4309 | new | Candidate Base.lproj/Main.nib named; labelled an observed convention; matches the decode_interface_builder NSKey |
| l_interface_builder_key_shape_inference | 4309 | new | A comment-less IB key is split as a key-shape inference, without a class |
| l_old_style_utf8_comments | 4309 | new | UTF-8 text keeps its comment and encoding; escaped quote decoded |
| l_old_style_utf16_be_bom | 4309 | new | UTF-16 BE with BOM: encoding, byte order and comment |
| l_old_style_bom_variants | 4309 | new | UTF-8 with BOM decoded; UTF-16 LE without BOM decoded with its byte order or reported as an encoding limitation |
| l_xml_plist_strings | 4309 | new | XML-plist .strings reported as XML |
| l_device_and_width_variants | 4309 | new | stringsdict device and width rules by key; String Catalog device variation |
| l_missing_other_category_flagged | 4309 | new | A plural without `other` is flagged |
| l_string_catalog_single_file | 4309 | new | Single .xcstrings: sourceLanguage, version, state, comment, catalog format, plural variants |
| l_single_strings_and_stringsdict_files | 4309 | new | Single .strings (UTF-16, IB comment) and .stringsdict targets |
| l_nested_bundle_inventory | 4309 | new | Nested appex table and development region |
| l_stickies_services_menu_binary | 4309 | new | Stickies fr ServicesMenu "Make Sticky" = "Créer une nouvelle note", binary plist |
| l_loctable_listed_not_decoded | 4309 | new | Stickies .loctable files listed as not decoded |
| l_invalid_file_limitation_others_kept | 4309 | new | Bad.strings named in a limitation; other tables still reported |
| l_duplicate_key_limitation | 4309 | new | Duplicate key reported with Dupes.strings |
| l_catalog_name_conflict_limitation | 4309 | new | Same-name .xcstrings/.strings pair reported |
| l_search_key_and_value_filters | 4309 | new | Key and value filters restrict entries; total and returned counts |
| l_entry_limit_reports_truncation | 4309 | new | An entry limit of 1 reports truncation |
| l_read_only_execution_free | 4309 | new | CLI and MCP runs succeed and leave the fixture bundle byte-identical |
| l_malformed_catalog_typed | 4309 | new | Malformed .xcstrings: a typed error or a limitation naming it; no stack trace |
| s_existing_inspect_plist_cli_mcp_shape | surface | existing | inspect_plist CLI and MCP agree; the output schema validates |
| s_existing_inspect_plist_typed_error | surface | existing | Missing target: typed error on CLI and MCP |
| s_existing_decode_interface_builder_nskey | surface | existing | decode_interface_builder reports NSKey `gfa-oA-9cr.title` |
| s_plist_section_cli_mcp_same_shape | surface | new | Interpreted section present with the same shape on CLI and MCP |
| s_plist_section_contract_validates | surface | new | Output schema and toolContract accept the section, which is declared in the schema; the not-applied result validates |
| s_privacy_cli_mcp_same_shape | surface | new | Privacy operation: same result shape and envelope on CLI and MCP |
| s_privacy_contract_validates_results | surface | new | Privacy operation: Ajv and toolContract accept the result; the schema declares its keys |
| s_privacy_errors_are_typed | surface | new | Privacy operation: typed errors on CLI and MCP |
| s_privacy_docs_topic_and_reference | surface | new | Privacy operation documented with PrivacyInfo.xcprivacy and required reason; `tool` paired with `rea cli` |
| s_privacy_readme_editions_consistent | surface | new | English README reaches it; translations consistent |
| s_localization_cli_mcp_same_shape | surface | new | Localization operation: same result shape and envelope on CLI and MCP |
| s_localization_contract_validates_results | surface | new | Localization operation: Ajv and toolContract accept the result; the schema declares its keys |
| s_localization_errors_are_typed | surface | new | Localization operation: typed errors on CLI and MCP |
| s_localization_docs_topic_and_reference | surface | new | Localization operation documented with .stringsdict and .xcstrings; `tool` paired with `rea cli` |
| s_localization_readme_editions_consistent | surface | new | English README reaches it; translations consistent |

Counts: 26 cases for 4302, 22 for 4304, 26 for 4309 and 15 for surface, 89 in total. 4 are `existing` and
85 are `new`.

## Validation

### Against the baseline checkout

The baseline is the checkout at `/Users/kelvin/bench2/clones/oracle`, branch `bench2/oracle`, head
`ad7d4c1f4`, built with `npm run build:cached`. Result: **89 total, 4 passed, 85 failed.**

- The 4 passing cases are exactly the `existing` cases:
  - `p_value_bundle_unchanged`
  - `s_existing_inspect_plist_cli_mcp_shape`
  - `s_existing_inspect_plist_typed_error`
  - `s_existing_decode_interface_builder_nskey`
- All 85 `new` cases fail on the baseline:
  - The #4302 cases fail because `normalized_result` has no interpreted section, and the limitations case
    because the baseline's limitations are empty.
  - The #4304 and #4309 cases, and their surface cases, fail with "operation not discoverable".

The run left the repository clean (`git status` showed no changes).

### Against a throwaway prototype

To show that the `new` cases can pass, I built a throwaway prototype in the session scratchpad. It was
outside the repository and outside ORACLE_DIR, and I deleted it afterwards.

The prototype was a mock worktree. Its `scripts/rea.mjs` wrapped the real baseline build: it forwarded
existing commands, post-processed `inspect-plist`, and proxied MCP stdio. It implemented the three features
in plain JavaScript using `plutil`, `vtool` and `dyld_info`, and it came with copied docs and README editions
that mention the new operations. It ran in two designs:

- **Design A:** new tools `inspect_privacy_manifests` and `inspect_localizations`, which take a `path`
  argument. **89/89 passed.**
- **Design B:**
  - the features are modes of existing session-bound tools: `inspect_artifact` with
    `view: "privacy_manifests"`, and `decode_interface_builder` with `content: "localizations"`, plus
    `key_filter`, `value_filter` and `max_entries`;
  - MCP needs `open_bundle_resources` first;
  - the result fields are renamed: `status`→`state`, `verdict`→`assessment`, `entries`→`rows`,
    `findings`→`diagnostics`, `source_format`→`encoding_info`, `defaulted`→`is_default_value`, and the
    section is `declarations` instead of `info_plist`.

  Discovery found both modes. **89/89 passed.**

Then 18 deliberate defects were injected into design A, one at a time. Each failed exactly the cases that
check that behaviour, and nothing else passed or failed differently:

| Defect | Cases that failed |
| --- | --- |
| `bg_status` | 2 background-mode cases |
| `ats_yes` | `p_ats_modern_arbitrary_loads_no` |
| `no_defaulted` | 3 defaulted-value cases |
| `statfs_declared` | 2 cross-check cases |
| `ambiguous_missing` | 2 cases |
| `applicability_required` | 2 cases |
| `no_comments` | 7 comment and IB cases |
| `fmt_xml` | 2 binary-format cases |
| `no_base_value` | 1 case |
| `listed_all` | 1 case |
| `drop_reason_findings` | 3 cases |
| `no_link` | `m_cross_check_fixture_executable` |
| `no_sha` | 4 digest cases |
| `plural_swap` | 1 case |
| `no_unrecognized` | 1 case |
| `no_unattributed` | 1 case |
| `no_limitations` | 7 limitation cases |
| `no_filter` | the search case |

The prototype only shows that a plausible implementation's output can satisfy the checks. Real
implementations are graded through their own CLI, MCP server, contracts and docs.

## Known limits of the oracle

- **Host.** The oracle needs macOS with Xcode (iPhoneOS SDK, `xcstringstool`, `ibtool`, `vtool`/`dyld_info`
  through the implementation). The Stickies and Calculator cases assume the stock apps that ship with
  macOS 26.x. If the Mac Catalyst fixture cannot be built, `m_mac_catalyst_applicability_unknown` fails with
  that reason.
- **Exact counts.** These cases follow the issue's wording: `m_three_invalid_findings` (exactly 3),
  `m_case_sensitive_data_type_clears_one` (exactly 2), and exactly one finding each for TN3183 and TN3184 in
  `m_structure_rules_tn3183_tn3184_tn3181`.
- **"Launches nothing."** `l_read_only_execution_free` checks only that the fixture bundle is unchanged. It
  cannot observe process launches.
