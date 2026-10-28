# Task: REA issues #4302, #4304 and #4309

Repository: REA (reverse-engineer-anything), checked out at the current working directory on its own branch.
Implement all three GitHub issues below in this repository.

## Issues (verbatim)

### Issue #4302: [Feature] Interpret Info.plist entry points and capabilities per bundle

### Before submitting

- [X] I searched the open and closed issues and pull requests for a related proposal.
- [X] I understand the [contribution scope](https://github.com/morluto/rea/blob/main/CONTRIBUTING.md#contribution-scope) must be reviewed before integration; open questions are welcome here.
- [X] This request does not include credentials, private samples, or sensitive target data.

### Area

Native analysis or decompilation

### Current limitation

For an Apple app and each nested bundle, `inspect_plist` should report the entry points and capabilities that bundle's Info.plist declares, with Apple's documented meaning: URL schemes, queried schemes, document types, type declarations, activity types, Services, background modes and tasks, usage descriptions, the extension point, and the effective App Transport Security (ATS) policy under Apple's precedence rules, including pinned domains. Platform- and device-suffixed variants fold into their base key with qualifiers visible. The raw plist `value` stays unchanged.

An analyst needs to know how other software can reach an app and its extensions, which background modes and protected resources it declares, and what network-security policy it configures. REA 6.5.0 (tag `rea-agents-6.5.0`, commit `3b79af28e80785afce9f5998d8f03289402a1847`) does not interpret any of these keys.

`inspect_plist` returns the plist as raw JSON `value` plus five `bundle` strings (identifier, executable, name, version, short version). `project_apple_application_graph` lists each bundle's `info_plist_path`, states that it does not parse plist semantics, and sends agents to `inspect_plist` "for CFBundleExecutable, identifiers, and declared services", which that tool returns only as raw JSON.

Observed with the release build on macOS 27.0.1 (26A434) arm64, Node.js v26.10.0, using a self-built `./fixture/Demo.app` (a copy of `/bin/ls` as `Contents/MacOS/Demo`) whose `Contents/Info.plist` declares `NSAppTransportSecurity` with `NSAllowsArbitraryLoads = true`, `NSAllowsArbitraryLoadsInWebContent = true` and an empty `NSExceptionDomains` entry for `example.com`, plus `UIBackgroundModes = [audio, teleport]` and `UISupportedInterfaceOrientations~ipad`:

```sh
npx -y rea-agents@6.5.0 inspect-plist ./fixture/Demo.app --json
```

The result echoes the ATS dictionary verbatim and reports `limitations: []`. Nothing says that Apple ignores `NSAllowsArbitraryLoads` on iOS 10 and later and macOS 10.12 and later when `NSAllowsArbitraryLoadsInWebContent` is present, or that `example.com` is exempt from the global keys. An agent reading `NSAllowsArbitraryLoads: true` can wrongly conclude that ATS is off everywhere. `teleport` is returned exactly like the documented mode `audio`, and `UISupportedInterfaceOrientations~ipad` appears as an unrelated top-level key. A nested appex's Info.plist can be selected with `--relative-path`, but its `NSExtensionPointIdentifier` is likewise a raw string.

### Use cases

_No response_

### Desired outcome and success criteria

`inspect_plist` (CLI `inspect-plist` and MCP) gains an interpreted section next to the unchanged `value` and `bundle`. Each result covers one bundle's Info.plist, named by `source_path`; to cover the app and every nested bundle, the agent inspects each `info_plist_path` that `project_apple_application_graph` lists, using the existing path or `--relative-path` input. The graph stays an execution-free inventory projection.

- Applicability: applied only to a file named exactly `Info.plist` with a dictionary root; for any other plist the section says it was not applied and why, so a launchd or preferences plist is never read as Info.plist declarations.
- Coverage: the section names the key families it interprets; a covered key that is absent is reported as not declared. Other keys stay only in `value`. Each item carries its exact source key path, the Apple key it was read as, and a short documented meaning.
- Entry points: `CFBundleURLTypes` (schemes, name, role), `LSApplicationQueriesSchemes`, `CFBundleDocumentTypes` (content types, name, role, document class, `LSHandlerRank`), `UTExportedTypeDeclarations` and `UTImportedTypeDeclarations` (identifier, conformances, tags; an identifier in both lists notes that the exported declaration takes precedence), `NSUserActivityTypes`, `NSServices` (menu item, message, port name, send and return types, key equivalent), and the `NSExtensionPointIdentifier` of `NSExtension`.
- Capabilities: `UIBackgroundModes`, `BGTaskSchedulerPermittedIdentifiers`, and each `NS*UsageDescription` key with the protected resource it names and the string as written in this file.
- Defaults: an absent `CFBundleTypeRole` or `LSHandlerRank` is reported with Apple's documented default (`None`, `Default`), marked defaulted rather than declared.
- Value status: values of keys with a closed documented set (`CFBundleTypeRole`, `LSHandlerRank`, `UIBackgroundModes`) are marked documented or outside Apple's documented values; none are dropped. The `NSExtensionPointIdentifier` key page is not exhaustive, so extension points are marked listed or not listed on that page, never invalid. A usage-description-shaped key that Protected resources does not list is reported as such.
- Fail closed: a covered key with the wrong type (for example `CFBundleURLSchemes` as a string) is reported as malformed with its raw value while the rest of the result succeeds; undocumented subkeys inside a covered entry are reported raw as undocumented.
- Suffix folding: a top-level `[key name]-[platform]~[device]` key whose qualifiers are among Apple's listed values is grouped under its base key, each variant keeping its qualifiers and exact source key, and covered keys are interpreted per variant. REA does not choose which variant applies at run time. A suffix with unlisted qualifiers is not folded and is reported as an unrecognized qualifier.
- ATS globals: for each documented OS range, the effective arbitrary-loads outcome and the rule that produced it. On iOS 9.0 and macOS 10.11, `NSAllowsArbitraryLoads` applies and the other global exceptions are ignored; on iOS 10 or later and macOS 10.12 or later, `NSAllowsArbitraryLoads` is ignored (`NO`) when `NSAllowsArbitraryLoadsForMedia`, `NSAllowsArbitraryLoadsInWebContent` or `NSAllowsLocalNetworking` is present. Other global keys are reported with their values.
- ATS domains: each exception domain's keys with documented defaults filled in and marked defaulted (`NSExceptionRequiresForwardSecrecy` `YES`, `NSExceptionMinimumTLSVersion` `TLSv1.2`), subdomain inclusion, and the statement that global keys do not apply to it, even when its dictionary is empty. Each `NSPinnedDomains` entry reports subdomain inclusion and its CA and leaf `SPKI-SHA256-BASE64` values, with the rule that when both are given ATS requires a match in each category.
- Limitations in the Evidence: these are declarations, not observed handlers, granted permissions or network behavior; ATS governs URL Loading System connections and is disabled for apps linked against an SDK older than iOS 9.0 or macOS 10.11, which a plist cannot establish; localized string overrides are not merged.

**Acceptance criteria**

- On `./fixture/Demo.app` with `CFBundleURLTypes` (scheme `demo`, role `Editor`), `LSApplicationQueriesSchemes = [othertool]`, a `CFBundleDocumentTypes` entry (rank `Owner`, role `Viewer`), `NSCameraUsageDescription` and `UIBackgroundModes = [audio, teleport]`, the section reports each declaration with its meaning, marks `audio` documented and `teleport` outside Apple's documented values, and `value` is identical to the release build's.
- On the same fixture's ATS dictionary (`NSAllowsArbitraryLoads` and `NSAllowsArbitraryLoadsInWebContent` both true, `example.com` mapped to an empty dictionary), the section reports global arbitrary loads as `NO` for iOS 10 or later and macOS 10.12 or later and `YES` for iOS 9.0 and macOS 10.11, names the rule for each, and reports `example.com` as exempt from global keys with forward secrecy `YES` and minimum TLS `TLSv1.2` marked defaulted.
- A fixture `NSPinnedDomains` entry with one CA and one leaf `SPKI-SHA256-BASE64` value is reported with both values and the match-each-category rule.
- `inspect-plist ./fixture/Demo.app --relative-path Contents/PlugIns/Share.appex/Contents/Info.plist` reports extension point `com.apple.share-services` as listed on the key page. A second appex declaring `com.apple.networkextension.packet-tunnel` is reported as not listed, not invalid. The app's own result does not include either.
- On stock `/System/Applications/Stickies.app`, the section reports the `NSServices` entry with message `makeStickyFromTextService` and its send types, reports its `NSExecutable` subkey raw as undocumented, and reports the document type's absent `LSHandlerRank` as `Default`, marked defaulted.
- On stock `/System/Applications/Calculator.app`, `UISupportedInterfaceOrientations~ipad` and `UISupportedInterfaceOrientations~iphone` are grouped under `UISupportedInterfaceOrientations` with device qualifiers `ipad` and `iphone` and their exact source keys.
- A fixture key `NSUserActivityTypes-amiga~toaster` is not folded and is reported as an unrecognized qualifier.
- A fixture with `CFBundleURLSchemes` as a string is reported as malformed with the raw value, and the tool still returns success with the rest of the section.
- Inspecting a copy of the fixture's Info.plist saved as `/tmp/rea-fixture/other.plist` (absolute path input) returns the section as not applied, with the reason, and the same `value` as the release build.
- The `inspect_plist` tool description and its documentation name the section and the key families it covers.

### Expected interaction with targets

Read existing files or captured data

### Target and scope details (optional)

**Affected area**

`inspect_plist` (CLI `inspect-plist` and MCP): its result schema, tool description and documentation. `project_apple_application_graph` output does not change.

**Non-goals**

- launchd, XPC service, privileged-helper, login-item and system-extension relationships between bundles.
- Privacy manifests (`PrivacyInfo.xcprivacy`) and their checks.
- Localized values of usage descriptions and other strings from `InfoPlist.strings` or string catalogs.
- Accepting nested bundle directories (`.appex`, `.framework`, `.xpc`) as direct targets; this request uses the existing relative-path selection from the app.
- Entitlements, associated domains and provisioning profiles.
- Judging whether a declaration is justified, or observing handlers, permissions or connections at run time.
- Interpreting Info.plist keys outside the listed families; they stay raw in `value`.

### Verification available so far

I can suggest a sample or manual check

### Verification details (optional)

_No response_

### Alternatives considered

- Per-bundle declaration nodes in `project_apple_application_graph`: rejected for this request, because the graph projects inventory Evidence (paths and hashes) and documents that it does not parse plist semantics; it would have to start reading plist bytes. `inspect_plist` already reads and decodes them.
- One call that interprets every bundle in an app at once: convenient, but it duplicates the graph's bundle discovery. Per-file results keep each answer tied to one exact source path.

### Possible direction

_No response_

### References

Apple documentation (corpus fetched 2026-09-30):

- Information property list: "The file name is case-sensitive and must begin with a capital letter `I`." https://developer.apple.com/documentation/bundleresources/information-property-list
- Managing your app’s information property list values: syntax "`[key name]-[platform]~[device]`", where "`platform` and `device` are optional qualifiers that restrict the value for the key to the platform or device"; platforms `freebsd hpux iphoneos linux macos solaris tvos visionos watchos windows`, devices `appletv applevision applewatch iphone ipad mac`. https://developer.apple.com/documentation/bundleresources/managing-your-app-s-information-property-list
- URL types: https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundleurltypes ; roles `Editor`, `Viewer`, `Shell`, `QLGenerator`, `None`, default `None`: https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundleurltypes/cfbundletyperole . Defining a custom URL scheme: "URL schemes offer a potential attack vector into your app" and "If multiple apps register the same scheme, the app the system targets is undefined." https://developer.apple.com/documentation/xcode/defining-a-custom-url-scheme-for-your-app
- `LSApplicationQueriesSchemes` has no key page in the corpus; canOpenURL says "you must declare the URL schemes you pass to this method by adding the `LSApplicationQueriesSchemes` key to your app’s `Info.plist` file". https://developer.apple.com/documentation/uikit/uiapplication/canopenurl(_:)
- Document types `CFBundleTypeRole`: values `Editor`, `Viewer`, `Shell`, `QLGenerator`, `None`, default `None`. https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundledocumenttypes/cfbundletyperole
- LSHandlerRank: "The ranking of this app among apps that declare themselves as editors or viewers of the given file type"; values `Owner`, `Default`, `Alternate`, `None`, default `Default`. https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundledocumenttypes/lshandlerrank
- Type declarations: https://developer.apple.com/documentation/bundleresources/information-property-list/utexportedtypedeclarations and https://developer.apple.com/documentation/bundleresources/information-property-list/utimportedtypedeclarations ; "If both exported and imported declarations for a type exist, the exported declaration takes precedence over the imported declaration." https://developer.apple.com/documentation/uniformtypeidentifiers/defining-file-and-data-types-for-your-app
- Other entry-point and capability keys: https://developer.apple.com/documentation/bundleresources/information-property-list/nsuseractivitytypes , https://developer.apple.com/documentation/bundleresources/information-property-list/nsservices (documented subkeys NSKeyEquivalent, NSMenuItem, NSMessage, NSPortName, NSTimeout, NSReturnTypes, NSSendTypes, NSUserData), https://developer.apple.com/documentation/bundleresources/information-property-list/uibackgroundmodes (14 possible values from `audio` to `voip`), https://developer.apple.com/documentation/bundleresources/information-property-list/bgtaskschedulerpermittedidentifiers , and Protected resources for usage descriptions: https://developer.apple.com/documentation/bundleresources/protected-resources
- NSAppTransportSecurity: global exceptions apply "except connections to domains specified in the NSExceptionDomains sub-dictionary"; on iOS 9.0 or macOS 10.11 ATS "ignores the other global exceptions"; "When you link your app against an older SDK, ATS is disabled no matter which version of operating system your app runs on." https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity
- NSAllowsArbitraryLoads: "In iOS 10 and later and macOS 10.12 and later, the value of the NSAllowsArbitraryLoads key is ignored—and the default value of `NO` used instead—if any of the following keys are present": NSAllowsArbitraryLoadsForMedia, NSAllowsArbitraryLoadsInWebContent, NSAllowsLocalNetworking. https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowsarbitraryloads
- NSExceptionDomains: "If you specify an exception domain dictionary, ATS ignores any global configuration keys, like NSAllowsArbitraryLoads, for that domain. This is true even if you leave the domain-specific dictionary empty". https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsexceptiondomains ; defaults `YES` (https://developer.apple.com/documentation/bundleresources/information-property-list/nsexceptionrequiresforwardsecrecy) and `TLSv1.2` (https://developer.apple.com/documentation/bundleresources/information-property-list/nsexceptionminimumtlsversion).
- NSPinnedDomains: "If you specify both, App Transport Security (ATS) requires a match in each category." https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nspinneddomains
- NSExtensionPointIdentifier lists 34 distinct `com.apple.` values in 35 rows (`com.apple.ctk-tokens` appears twice), including `com.apple.share-services`: https://developer.apple.com/documentation/bundleresources/information-property-list/nsextension/nsextensionpointidentifier ; the packet tunnel provider page uses `com.apple.networkextension.packet-tunnel`, which that list omits: https://developer.apple.com/documentation/networkextension/nepackettunnelprovider

REA source at the release tag; main `d52a488` changes only default plist path resolution in these files:

- Five-field bundle projection: https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/native/parsers/plist.ts#L252-L266
- Result schema: https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/domain/native/nativeInspection.ts#L162-L175
- Tool contract: https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/contracts/native/nativeToolContracts.ts#L111-L117
- Graph limitations: https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/domain/apple/appleApplication.ts#L448-L449
- Graph documentation ("It does not parse plist or CMS semantics"): https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/docs/apple-application-analysis.md#L3-L8
- Per-bundle `info_plist_path`: https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/domain/apple/appleBundleAnatomy.ts#L21-L43

Related: merged PR #3774 changed how `inspect_plist` picks its default plist; this request adds interpretation and leaves selection alone. Searches for Info.plist URL schemes, `CFBundleURLTypes`, App Transport Security, usage descriptions, `NSExtensionPointIdentifier` and `inspect_plist` semantics found no existing request.


### Issue #4304: [Feature] Inspect privacy manifests and cross-check required-reason API imports per bundle

### Before submitting

- [X] I searched the open and closed issues and pull requests for a related proposal.
- [X] I understand the [contribution scope](https://github.com/morluto/rea/blob/main/CONTRIBUTING.md#contribution-scope) must be reviewed before integration; open questions are welcome here.
- [X] This request does not include credentials, private samples, or sensitive target data.

### Area

Native analysis or decompilation

### Current limitation

Add an execution-free Apple operation that finds every `PrivacyInfo.xcprivacy` in an app by bundle location, validates it against Apple's closed value sets and validity rules, and cross-checks each Mach-O's imported required-reason API symbols against the manifest of the bundle that contains it, reporting matches, missing declarations, and unknowns, qualified by platform.

An analyst examining an iOS or macOS app wants to know what each component declares about tracking, collected data, and required-reason APIs, whether those declarations are valid, and whether each bundle's code imports covered APIs its manifest does not declare. REA cannot answer any part of this.

At release `rea-agents-6.5.0` (commit `3b79af28e80785afce9f5998d8f03289402a1847`), `git grep -i` for `xcprivacy`, `NSPrivacy`, `PrivacyInfo`, and `privacy manifest` finds nothing, and current `main` adds nothing. The reachable pieces do not interpret the file:

- `inspect_plist` parses a manifest when the caller passes its path, but it returns the decoded value verbatim and only projects five `CFBundle*` keys ([NativeMacOSProvider.ts#L819-L834](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/native/NativeMacOSProvider.ts#L819-L834), [plist.ts#L252-L266](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/native/parsers/plist.ts#L252-L266)).
- Artifact inventory classifies a `.xcprivacy` file as a generic `resource` ([ArtifactGraphConstruction.ts#L463-L466](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/artifacts/inventory/ArtifactGraphConstruction.ts#L463-L466)), or as `framework` when its path lies inside a `.framework/` directory ([L456-L457](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/artifacts/inventory/ArtifactGraphConstruction.ts#L456-L457)), and the Apple application graph states that it "reports only exact paths and hashes" ([appleApplication.ts#L449](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/domain/apple/appleApplication.ts#L449)).
- `inspect_macho` lists imports, marked `exhaustive: false` ([NativeMachoInspection.ts#L279-L281](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/native/NativeMachoInspection.ts#L279-L281)), but nothing relates them to a manifest.

Observed on macOS 27.0.1 (26A434) arm64, Xcode 27.0 (27A266a), `npx -y rea-agents@6.5.0` (`--version` prints `6.5.0`): a self-built iOS-family `./fixture/Fixture.app` whose executable imports `_stat`, `_statfs`, and `_mach_absolute_time` has a root manifest with `NSPrivacyTracking` `true` and empty `NSPrivacyTrackingDomains`, the data type `NSPrivacyCollectedDataTypePhotosOrVideos` (Apple spells it `PhotosorVideos`), and the DiskSpace reason `85F4.1` under `NSPrivacyAccessedAPICategoryFileTimestamp`. `rea inspect-plist ./fixture/Fixture.app --relative-path PrivacyInfo.xcprivacy` returned the dictionary unchanged with `"limitations":[]` and no verdict on any of the three invalid values; `rea inspect-macho ./fixture/Fixture.app` listed the imports with no sign that `_statfs` (DiskSpace) is undeclared.

### Use cases

_No response_

### Desired outcome and success criteria

A new operation or a mode of an existing Apple tool (maintainers' choice), on CLI and MCP, accepts an app directory or IPA and returns one Evidence record with coverage, limitations, and provenance:

- **Bundles and manifests.** For the app and each nested code bundle REA already recognizes (frameworks, app extensions, XPC services, plug-ins), report the path, kind, platform from Mach-O build metadata, the expected manifest location per Apple's location table (bundle root on iOS-family platforms, `Contents/Resources/` for macOS apps, `Versions/<current>/Resources/` for macOS frameworks), and whether a manifest is there (path, SHA-256) or absent. A `PrivacyInfo.xcprivacy` anywhere else is listed as unattributed.
- **Validation.** Per finding: key path, observed value, verdict, and the Apple rule applied. Rules: top-level types; `NSPrivacyTracking` and `NSPrivacyTrackingDomains` consistency; domain shape; accessed-API dictionaries with exactly two keys, a known category, and a non-empty reasons array valid for that category; collected-data dictionaries with exactly four keys, a known data type, Boolean `Linked` and `Tracking`, and known purposes. Matching is exact and case-sensitive (`NSPrivacyCollectedDataTypePhotosorVideos` is valid; `PhotosOrVideos` is not). Because Apple updates its lists, an unknown value is reported as "not in the documented value set" with the table's source and date. A file that does not decode as a property list is an invalid property list with no per-key verdicts.
- **Required-reason cross-check.** For each Mach-O slice, map imported C symbols to categories (normalizing variants such as the x86_64 `$INODE64` suffix, observed with Xcode 27 clang) and compare them with the manifest of the containing bundle; a loose dylib is covered by the bundle that contains it. Each hit is `declared`, `missing`, `category_ambiguous`, or `not_established`. `getattrlist`, `fgetattrlist`, and `getattrlistat` belong to both FileTimestamp and DiskSpace, so a hit lists both: `missing` when neither is declared, `category_ambiguous` when only one is. A declared category with no matching import is "declared, no import observed", not an error.
- **Unknowns stay unknown.** Property-level Swift and Objective-C APIs in Apple's lists (`UserDefaults`, `creationDate`, `systemUptime`, `activeInputModes`, volume-capacity URL keys) cannot be established from imports. They are `not_established`; an indicative reference may appear as a hint, never as `declared` or `missing`.
- **Platform qualification.** Required-reason results carry applicability: `required` for iOS, iPadOS, tvOS, visionOS, and watchOS; `not_required` for macOS, with a limitation quoting Apple's platform list; `unknown` for absent or conflicting build metadata and for Mac Catalyst, which Apple's list does not name. Validity and collected-data results apply on every platform.
- **Stated limitations.** Imports are not exhaustive (no `dlsym` lookups, inline system calls, or stripped metadata), and statically linked SDK code merges into its containing executable, so a hit cannot be attributed to the SDK.

**Acceptance criteria**

Fixture, built with stock Xcode tools (`dyld_info -imports` confirms each import):

```sh
xcrun --sdk iphoneos clang -target arm64-apple-ios17.0 -o ./fixture/Fixture.app/Fixture main.c   # calls stat, statfs, mach_absolute_time
xcrun --sdk iphoneos clang -target arm64-apple-ios17.0 -dynamiclib -o ./fixture/Fixture.app/Frameworks/Lib.framework/Lib lib.c   # calls getattrlist; no manifest
clang -target arm64-apple-macos14 -o ./fixture/Mac.app/Contents/MacOS/Mac main.c   # no manifest
```

- [ ] On `./fixture/Fixture.app`, the app's manifest is found at the root and `Lib.framework`'s is absent. Moving the app's manifest to `Fixture.app/Resources/` makes it absent and lists the moved file as unattributed.
- [ ] With the manifest from Current limitation, three invalid findings appear with key path, value, and rule: tracking `true` with empty domains (TN3181), `NSPrivacyCollectedDataTypePhotosOrVideos` outside the documented set, and `85F4.1` invalid for FileTimestamp. Changing the data type to `NSPrivacyCollectedDataTypePhotosorVideos` clears only that finding.
- [ ] A third key in an accessed-API dictionary, a collected-data dictionary without `NSPrivacyCollectedDataTypeTracking`, and an empty reasons array each produce one finding citing TN3183, TN3184, or TN3181 respectively.
- [ ] For `Fixture`, `_mach_absolute_time` is `declared` (SystemBootTime), `_stat` is `declared` (FileTimestamp, with that entry's invalid-reason finding linked), and `_statfs` is `missing` (DiskSpace). For `Lib`, `_getattrlist` is `missing` with candidates FileTimestamp and DiskSpace. After a root manifest declaring only FileTimestamp `C617.1` is added to `Lib.framework`, `_getattrlist` becomes `category_ambiguous`.
- [ ] Declaring `NSPrivacyAccessedAPICategoryUserDefaults` with `CA92.1` yields "declared, no import observed" with a `not_established` limitation and no error.
- [ ] For `./fixture/Mac.app`, the same imports are reported with applicability `not_required` and a limitation naming the iOS-family platforms; a manifest at `Contents/Resources/PrivacyInfo.xcprivacy` is found and validated.
- [ ] Rebuilding `Mac` with `-target x86_64-apple-macos11` (imports `_stat$INODE64` and `_statfs$INODE64`) yields the same category mapping as the arm64 build.
- [ ] A manifest replaced with non-plist bytes yields one invalid-property-list finding and no per-key verdicts; other bundles are still reported.
- [ ] Every result names the value-set source and date and states the import-coverage and static-linking limitations.

### Expected interaction with targets

Read existing files or captured data

### Target and scope details (optional)

**Affected area**

Apple static analysis on the MCP and CLI surfaces: Mach-O import and build-metadata reading, nested-bundle discovery, and a new typed privacy-manifest Evidence result. Validation and the import cross-check stay one issue because both rest on the same per-bundle manifest attribution and Evidence record, and the cross-check links each `declared` hit to that entry's validation findings.

**Non-goals**

- Interpreting general `Info.plist` keys, including `NS*UsageDescription` privacy strings.
- Accepting nested bundle directories as direct targets of the existing single-file tools, and flagging Mach-O code outside documented bundle code locations.
- App Store policy judgments beyond the documented validity rules, such as whether a declared reason truthfully describes use, SDK signature requirements, or ITMS submission outcomes.
- Identifying third-party SDKs or applying Apple's list of SDKs that need a manifest.
- XCFramework distribution bundles, Swift package source trees, and Xcode's privacy report.
- Observing API calls at runtime or launching the target.

### Verification available so far

I can suggest a sample or manual check

### Verification details (optional)

_No response_

### Alternatives considered

- Run `inspect_plist` on each manifest path and `inspect_macho` on each Mach-O, then compare by hand. The analyst must apply Apple's location table, value sets, and reason codes per bundle, and nothing records the result as Evidence.
- Use Xcode's Generate Privacy Report. It aggregates manifests from an Xcode archive in the Organizer, which an analyst of a shipped bundle usually lacks, and it does not compare manifests with imported symbols.

### Possible direction

_No response_

### References

- [Privacy manifest files](https://developer.apple.com/documentation/bundleresources/privacy-manifest-files): `PrivacyInfo.xcprivacy` "is the required file name for bundled privacy manifests"; collected data types are required "on all platforms" and required reasons APIs "on iOS, iPadOS, tvOS, visionOS, and watchOS".
- [Adding a privacy manifest to your app or third-party SDK](https://developer.apple.com/documentation/bundleresources/adding-a-privacy-manifest-to-your-app-or-third-party-sdk): iOS-family apps and frameworks place it "at the root", macOS and Mac Catalyst apps in `Contents/Resources/`, macOS frameworks in `Versions/A/Resources/`; "All privacy manifest files in your app must be valid, containing only the expected keys and values."
- [Describing use of required reason API](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api): "For each executable or dynamic library in an app that uses a required reason API, the bundle that includes the executable or dynamic library needs to include a privacy manifest file that reports the API." Apple "will update this article from time to time."
- [Placing content in a bundle](https://developer.apple.com/documentation/bundleresources/placing-content-in-a-bundle): its privacy manifest row gives `Contents/Resources/`, `Versions/A/Resources/`, and `/` for macOS, macOS framework, and iOS-family bundles.
- [NSPrivacyAccessedAPIType](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacyaccessedapitypes/nsprivacyaccessedapitype) and [NSPrivacyAccessedAPITypeReasons](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacyaccessedapitypes/nsprivacyaccessedapitypereasons): five categories and seventeen reason codes. FileTimestamp covers `getattrlist`, `getattrlistbulk`, `fgetattrlist`, `getattrlistat`, `stat`, `fstat`, `fstatat`, `lstat`; DiskSpace covers `statfs`, `statvfs`, `fstatfs`, `fstatvfs`, `getattrlist`, `fgetattrlist`, `getattrlistat`; SystemBootTime covers `systemUptime` and `mach_absolute_time()`.
- [NSPrivacyCollectedDataType](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacycollecteddatatypes/nsprivacycollecteddatatype) and [NSPrivacyCollectedDataTypePurposes](https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacycollecteddatatypes/nsprivacycollecteddatatypepurposes): 35 data types, including `NSPrivacyCollectedDataTypePhotosorVideos`, and six purposes.
- [TN3181](https://developer.apple.com/documentation/technotes/tn3181-debugging-invalid-privacy-manifest): invalid means "invalid keys or values" or "an improperly formatted property list file"; it gives the tracking and domain consistency rules (a domain "Contains no path and query components" and "Contains no trailing slash") and the empty and mismatched reasons rules.
- [TN3183](https://developer.apple.com/documentation/technotes/tn3183-adding-required-reason-api-entries-to-your-privacy-manifest): the accessed-API dictionary "contains exactly two keys". [TN3184](https://developer.apple.com/documentation/technotes/tn3184-adding-data-collection-details-to-your-privacy-manifest): the collected-data dictionary "contains exactly four keys".
- REA already recognizes nested bundle extensions by path ([appleBundleAnatomy.ts#L8-L9](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/domain/apple/appleBundleAnatomy.ts#L8-L9)).
- Related: #3289 asks broadly for iOS application support; this is one concrete static capability for both iOS and macOS bundles. GitHub search finds no issue or PR about privacy manifests, `xcprivacy`, or required-reason APIs.


### Issue #4309: [Feature] Inventory .lproj localizations and decode string tables per bundle

### Before submitting

- [X] I searched the open and closed issues and pull requests for a related proposal.
- [X] I understand the [contribution scope](https://github.com/morluto/rea/blob/main/CONTRIBUTING.md#contribution-scope) must be reviewed before integration; open questions are welcome here.
- [X] This request does not include credentials, private samples, or sensitive target data.

### Area

Native analysis or decompilation

### Current limitation

Add an execution-free Apple operation that inventories each bundle's `.lproj` localizations and decodes its `.strings` (old-style text, XML plist, binary plist), `.stringsdict`, and `.xcstrings` (String Catalog JSON) files into keyed string tables. Each entry reports table name, key, value, localization, source file, source format, translator comment, and plural or device variants. Interface Builder tables also report the object ID and property that the key names, and `InfoPlist` tables report which Info.plist key each entry localizes. The result supports two analyst tasks: searching user-visible text across localizations, and mapping a UI string to the nib object and code that display it.

An analyst who sees a string in an app's UI, such as a menu title, a permission prompt, or a localized app name, wants to know which table and key it comes from, which nib object shows it, and how it reads in each shipped language. REA cannot answer this as one question today.

At release `rea-agents-6.5.0` (commit `3b79af28e80785afce9f5998d8f03289402a1847`), `git grep` over `src/` for `lproj`, `stringsdict`, `xcstrings`, and `InfoPlist.strings` finds only one test fixture path (`Base.lproj/Main.nib`), and current `main` adds no localization handling. What the release build does with these files:

- Artifact inventory gives `.strings` no path role, so it falls to the generic `resource` role ([ArtifactGraphConstruction.ts#L445-L467](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/artifacts/inventory/ArtifactGraphConstruction.ts#L445-L467)). Content sniffing turns binary-plist `.strings` and `.stringsdict` into `plist` ([L470-L494](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/artifacts/inventory/ArtifactGraphConstruction.ts#L470-L494), [L523](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/artifacts/inventory/ArtifactGraphConstruction.ts#L523)). No keys or values are reported, and nothing records which localization or table a file belongs to.
- `inspect_plist` decodes one file at a time when the caller supplies its path. It returns the raw value through `plutil`, so translator and Interface Builder comments are lost. It reports a UTF-16 old-style text `.strings` file as `format: "xml"` because any `file` output containing "text" maps to `xml` ([NativeMacOSProvider.ts#L819-L824](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/native/NativeMacOSProvider.ts#L819-L824)). That format label is reported separately in #4294.
- `decode_interface_builder` already returns `NSLocalizableString` objects whose raw `nibValues` carry `NSKey` (for example `"7Rn-sG-NZb.title"` in stock Stickies' `MainMenu.nib`) and `NSDev` (the development-language text) ([InterfaceBuilderAnalysis.ts#L490-L512](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/artifacts/apple/InterfaceBuilderAnalysis.ts#L490-L512)). The node's `interface_builder_object_id` is the archive object index, not the authored object ID ([interfaceBuilderGraph.ts#L117-L121](https://github.com/morluto/rea/blob/3b79af28e80785afce9f5998d8f03289402a1847/src/domain/apple/interfaceBuilderGraph.ts#L117-L121)). Nothing connects those keys to a localized table.

Observed on macOS 27.0.1 (26A434) arm64 with Xcode 27.0 (27A266a) and `npx -y rea-agents@6.5.0` (`--version` prints `6.5.0`), using the fixture under Acceptance criteria. `inspect-artifact ./Fixture.app` lists the binary `.strings` and `.stringsdict` files as `plist` and `fr.lproj/Main.strings` (UTF-16 text) as `resource`, and its output contains none of `Bonjour`, `Démo`, or `gfa-oA-9cr`. `inspect-plist --relative-path Contents/Resources/fr.lproj/Main.strings` returns `{"abc-12-xyz.title":"Main","gfa-oA-9cr.title":"Scan Document"}` with `format: "xml"` and without the `/* Class = "NSMenuItem"; ... ObjectID = "gfa-oA-9cr"; */` comments. `decode-interface-builder ./Fixture.app` reports an `NSLocalizableString` node with `NSKey: "gfa-oA-9cr.title"` for `Base.lproj/Main.nib`.

### Use cases

_No response_

### Desired outcome and success criteria

A new read-only operation, or a mode of an existing Apple tool (maintainers' choice), is available on CLI and MCP. It accepts an app bundle or a directory, or a single `.strings`, `.stringsdict`, or `.xcstrings` file, and returns one Evidence record with coverage, limitations, and provenance:

- **Localization inventory per bundle.** For the target bundle and each nested bundle REA already recognizes, report its `CFBundleDevelopmentRegion` and `CFBundleLocalizations` values as declared (raw, absent stays absent), the observed `.lproj` directories including `Base.lproj`, and the table files in each one.
- **Entries.** One entry per key with bundle path, localization, table name (file base name), key, value, source file, and source format. The source format distinguishes old-style text (with its encoding: UTF-8 or UTF-16 and byte order), XML plist, binary plist, and String Catalog JSON. Old-style text keeps its comments. String Catalog entries keep `comment`, per-localization `state`, and the catalog's `sourceLanguage` and `version`.
- **Variants.** `.stringsdict` entries report `NSStringLocalizedFormatKey`, each variable's `NSStringFormatSpecTypeKey` and `NSStringFormatValueTypeKey`, and their plural categories (`zero`, `one`, `two`, `few`, `many`, `other`). Device (`NSStringDeviceSpecificRuleType`) and width (`NSStringVariableWidthRuleType`) rules are reported by their keys. String Catalog `variations` (plural, device) are reported the same way. A missing `other` category is flagged.
- **Interface Builder keys.** When a key has the form `<ObjectID>.<property>` and a comment `/* Class = "..."; ... ObjectID = "..."; */` is present, the entry reports `object_id`, `property`, and `class`. Without a comment, the split is reported as a key-shape inference. A Base-localized nib with the same table name is named as the candidate document. The match to `decode_interface_builder`'s `NSKey` is an observed convention, not documented Apple behavior, and is labeled that way.
- **InfoPlist tables.** Entries from `InfoPlist` tables carry the bundle's base Info.plist value for the same key, if present, without interpreting it.
- **Search.** Optional key and value filters, with total and returned counts. Limits on files and entries report truncation and never drop data silently.
- **Fail closed per file.** An undecodable file, an unsupported encoding, a duplicate key within one file, or a String Catalog that shares its name with a `.strings` or `.stringsdict` file in the same bundle each produce a limitation naming the file. Other tables are still reported. Files the operation sees but does not decode, such as `.loctable`, are counted and listed, never omitted.

**Acceptance criteria**

Fixture, built with stock Xcode tools. `src/Localizable.xcstrings` holds `greeting` (en `Hello`, fr `Bonjour`) and `%lld files` with en/fr plural variants `one`/`other`. `src/InfoPlist.xcstrings` holds fr `CFBundleDisplayName` `Démo` and `NSCameraUsageDescription`. `src/Base.lproj/Main.xib` is a macOS xib with one `NSMenuItem` titled `Scan Document`, id `gfa-oA-9cr`. `Info.plist` sets `CFBundleDevelopmentRegion` `en` and `CFBundleDisplayName` `Demo`.

```sh
A=./Fixture.app/Contents; mkdir -p $A/MacOS $A/Resources/Base.lproj; cp /bin/ls $A/MacOS/Fixture
xcrun xcstringstool compile src/Localizable.xcstrings -o $A/Resources --serialization-format binary
xcrun xcstringstool compile src/InfoPlist.xcstrings -o $A/Resources --serialization-format binary
xcrun ibtool --compile $A/Resources/Base.lproj/Main.nib src/Base.lproj/Main.xib
xcrun ibtool --export-strings-file $A/Resources/fr.lproj/Main.strings src/Base.lproj/Main.xib   # UTF-16 text with IB comments
```

- [ ] On `./Fixture.app`, localizations `Base`, `en`, and `fr` are reported with development region `en`. Tables are reported as `Localizable` (en, fr), `InfoPlist` (fr), and `Main` (fr).
- [ ] fr `Localizable` / `greeting` = `Bonjour`, source format binary plist.
- [ ] fr `%lld files` reports format key `%#@value@`, variable `value` with `NSStringPluralRuleType` and value type `lld`, and categories `one` = `%lld fichier` and `other` = `%lld fichiers`.
- [ ] fr `InfoPlist` / `CFBundleDisplayName` = `Démo`, with base Info.plist value `Demo`.
- [ ] fr `Main` / `gfa-oA-9cr.title` reports source format old-style text UTF-16 little-endian, `class` `NSMenuItem`, `object_id` `gfa-oA-9cr`, and `property` `title`. The key equals the `NSKey` of an `NSLocalizableString` node that `decode_interface_builder` reports for `Base.lproj/Main.nib`.
- [ ] Given `src/Localizable.xcstrings` directly, entries report source language `en`, per-localization state `translated`, and the same plural variants as the compiled `.stringsdict`.
- [ ] On `/System/Applications/Stickies.app` (macOS 27.0.1), each of its `ServicesMenu.strings` files is decoded as a binary plist. fr `Make Sticky` = `Créer une nouvelle note`. Its `.loctable` files are listed as not decoded.
- [ ] A `.strings` file with invalid content produces a limitation naming it, and the other tables are still reported. A same-name `.xcstrings` plus `.strings` pair in one bundle is reported with a limitation.
- [ ] The operation launches nothing and writes nothing.

### Expected interaction with targets

Read existing files or captured data

### Target and scope details (optional)

**Affected area**

Apple static analysis: artifact inventory and bundle resources, the Apple tool family next to `decode_interface_builder` and `inspect_asset_catalog`, the CLI and MCP contracts, and the native investigation guide.

**Non-goals**

- Interpreting Info.plist key semantics, such as usage-description meaning or entry points.
- Translating, machine-translating, editing, or recompiling strings or catalogs, or any source `.xcstrings` editing workflow.
- Decoding `Assets.car` or nib archives themselves, beyond reading the existing `decode_interface_builder` output for the join.
- Apple's `.loctable` files (binary plists keyed by localization, observed in stock macOS apps such as Stickies, with no documentation found in Apple's developer documentation), beyond listing them as not decoded. These could be a follow-up.
- Runtime string lookup, locale fallback resolution, or proof that code ever requests a given key.

### Verification available so far

I have a prototype or observed results

### Verification details (optional)

_No response_

### Alternatives considered

- Extend `inspect_plist`: it drops comments, has no table or localization context, and needs one call per file per language.
- Add string nodes to the Apple application graph: graphs suit relationships, not thousands of text rows; a graph could reference this Evidence later.

### Possible direction

_No response_

### References

Apple documentation (contract):

- The archived Information Property List Key Reference says: "Localized values are not stored in the `Info.plist` file itself. Instead, you store the values for a particular localization in a strings file with the name `InfoPlist.strings`" (https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/AboutInformationPropertyListFiles.html).
- The archived "Managing Strings Files Yourself" says Xcode "inserts a comment before each key-value pair that identifies the view that displays the text", for example `/* Class = "NSTextFieldCell"; title = "Address:"; ObjectID = "gfa-oA-9cr"; */` followed by `"gfa-oA-9cr.title" = "Address:";` (https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPInternational/MaintaingYourOwnStringsFiles/MaintaingYourOwnStringsFiles.html).
- The build settings reference (https://developer.apple.com/documentation/xcode/build-settings-reference) defines three relevant settings. `STRINGS_FILE_OUTPUT_ENCODING`: "Specify the output encoding to be used for Strings files - the default is UTF-16." `STRINGS_FILE_INFOPLIST_RENAME`: "renames .strings files whose basename matches that of the target's Info.plist file, to InfoPlist.strings in the built product." `LOCALIZATION_PREFERS_STRING_CATALOGS`: "string tables generated in a localization export will prefer the String Catalog format."
- "Localizing and varying text with a string catalog" names `Localizable.xcstrings` as the default and covers "plural variants" and "Vary by Device" (https://developer.apple.com/documentation/xcode/localizing-and-varying-text-with-a-string-catalog).
- "Localizing strings that contain plurals" defines `NSStringLocalizedFormatKey` and `NSStringFormatSpecTypeKey`, and says "The `other` category is a requirement" (https://developer.apple.com/documentation/xcode/localizing-strings-that-contain-plurals). "Creating width and device variants of strings" defines `NSStringDeviceSpecificRuleType` and `NSStringVariableWidthRuleType` (https://developer.apple.com/documentation/xcode/creating-width-and-device-variants-of-strings).
- `CFBundleDevelopmentRegion`: "The default language and region for the bundle, as a language ID", default `en-US` (https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundledevelopmentregion). `CFBundleLocalizations`: "The localizations handled manually by your app" (https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundlelocalizations).
- Xcode 15 release notes: "The build system will now consistently produce an error if a String Catalog coexists with .strings or .stringsdict files of the same name, within the same target" (https://developer.apple.com/documentation/xcode-release-notes/xcode-15-release-notes). Xcode 26 release notes: catalogs using comment or symbol generation "will auto-update their version number to 1.1" (https://developer.apple.com/documentation/xcode-release-notes/xcode-26-release-notes).

Documented versus observed:

- Apple's pages do not state that `.xcstrings` compiles to `.strings` and `.stringsdict`. The same-name conflict error is the strongest documented hint. Observed with Xcode 27.0: `xcstringstool compile` emits `<lang>.lproj/<table>.strings` and `.stringsdict`, as XML plists by default (not old-style text) or binary plists with `--serialization-format binary`. A device variant compiles to `NSStringDeviceSpecificRuleType` in `.stringsdict`, with the `other` value in `.strings`.
- The `.xcstrings` JSON schema (`sourceLanguage`, `strings`, `localizations`, `stringUnit`, `variations`) is not documented in the pages above. It is observed from Xcode-produced files.
- Shipped `.strings` files come in several encodings: old-style text in UTF-16 (the documented build default; `ibtool --export-strings-file` writes UTF-16 little-endian), XML plists, and binary plists. Stock Stickies on macOS 27.0.1 ships 42 `ServicesMenu.strings` files, all binary plists.
- Compiled nib internals are undocumented. Nibs that Xcode 27.0 compiles from `Base.lproj` xibs (AppKit and UIKit) contain `NSLocalizableString` objects whose `NSKey` equals the IB table key; a UIKit nib carries `NSKey: "Lbl-aa-123.text"`. So the join key works on these samples but is not an Apple contract.

Related: #480 (closed) delivered compiled nib decoding, and this request consumes its output without changing it. #496 (closed) delivered asset catalog inspection, including rendition localization, which covers a different file type. Searches for "stringsdict", "xcstrings", "lproj", "localization", and "InfoPlist.strings" found no matching issue or PR.


## Acceptance criteria

The work is accepted when all of the following hold on the final tree:

1. Each issue's desired outcome and success criteria are met. Where an issue leaves a choice to the maintainers
   (a new operation or a mode of an existing tool), make one choice and apply it consistently.
2. Every new operation, and every existing operation whose result changes, has:
   - a CLI entry point and an MCP tool, with the same request and result shapes;
   - a tool contract (request and result schemas, effects classification, examples where the project keeps them);
   - user-facing documentation, including the topic guide, the CLI and MCP reference, and the translated README
     editions where the English README lists capabilities;
   - boundary tests that cover the new behaviour and its failure paths (see docs/testing.md "Boundary regressions").
3. These commands all exit 0: `npm run check:fast`, `npm run format:check`, `npm run knip`,
   `npm run knip:production`, `npm run metadata:check`, `npm run docs:check`, `npm run licenses:check`,
   `npm run mcp-registry:check`, `npm run test:python`, `npm run jscpd`, `npm run scan:todos`.
4. The repository's existing real Apple verifiers keep passing.
5. Do not run the full test suite (`npm test`, `npm run test:run`, `npm run test:with-build`) or
   `npm run check:pr`. Run the specific test files you add or change (`npm run test:local -- <file>` for
   source tests, `npm run test:focused -- <file>...` for tests/boundary and tests/acceptance).

## Environment

- Node 24 is first on PATH; `node_modules` is installed. Run plain `npm run ...`.
- macOS host with Xcode command-line tools: `xcrun`, `plutil` and `codesign` are available.
- No network access is needed; do not add dependencies.
