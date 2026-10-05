# Design and review map — `wpt`

**Scope:** unpublished work after 0.6.4. This is a code/evidence map, not a certification of every README claim. Preserve command, exit, fixture, source revision and remaining limits for each release claim.

## Current data flow

1. `wpt/cli.py` and `wpt/gui.py` resolve the ableton-linux prefix and vendor MSI; wrappers may need Wine, so `--dry-run` is not proof of external side-effect absence.
2. `wpt/msi.py:file_manifest` reads Directory/Component/File rows and retains each file's root, relative target path, key, integer size, Component Attributes and Condition. A target `DefaultDir` strips the `:source` half; `.` adds no segment. Missing parents, cycles, invalid names and duplicate case-insensitive destinations refuse the manifest. The third `msiinfo export` metadata line is not a row.
3. Both payload and tables-only routes use `installer._plan_from` to map rows to absolute paths beneath selected roots. A root/self-referential action or an action with no mapped file refuses the plan. Payload plans compare selected extracted paths and sizes with the manifest; tables-only plans have no cabinet and cannot install. Install planning refuses unsupported optional, conditioned, source-only, shared-reference, permanent, transitive, `Shared` or `NeverOverwrite` component semantics because installed feature/component state is unavailable; FeatureComponents/feature-level selection is not modelled.
4. `verify_plan` and `filter_needing_repair` require the active prefix environment and use full-path File-table sizes, including bundle internals, duplicate basenames and zero-byte files. Their case-insensitive path walk refuses symlinks and ambiguous spellings. Repair selects damaged individual files, not their whole bundle. Repair and inventory remain size-based; same-size edits can remain undetected.
5. `inventory.build` indexes full destination paths and retains ProductCode ownership for cross-product collisions. Uninstall ownership checks can include non-plugin Installer-cache MSIs and map application/driver roots. Unknown/unmapped or unreadable cached MSI data makes the scan incomplete and refuses registered removal rather than guessing; this conservative gate can block removal because of an unrelated cached MSI. `scan` separately compares registry paths with disk; neither verdict proves the other.
6. Before a registered uninstall, CLI and GUI stage/read the selected MSI and scan cached MSI ownership. Known cross-product ownership, an incomplete ownership scan, a registered tables-only plan, an unmapped root, or a symlink/ambiguous planned prefix path blocks the default uninstall before preset rescue, Wine `msiexec`, direct removal or registry purge. Fallback Manufacturer/ProductName values are validated as single path components. A non-zero `msiexec` result, or a zero exit while registration remains or cannot be read, also stops follow-on direct removal and purge. Unregistered products skip Wine. `--no-files` remains a separate user-authorized path through Wine, skips shared-file ownership checks, and is not bounded by the toolkit's direct-removal guarantees.
7. Preset rescue runs before Wine and strictly traverses planned no-clobber paths plus recognized Roaming/MIDI XML paths; scan/stat errors propagate and symlink directories are not followed. Every enumerated no-clobber file needs a successful rescue row or the job stops. Roaming/MIDI lookup is limited to inferred product-name paths; uninspected RemoveFile/custom actions mean discovery cannot prove a complete backup. Keep an independent backup because Wine can delete user files or affect host-mapped paths.
8. Payload-backed direct removal compares eligible files against staged MSI bytes and declared size. It preserves modified, no-clobber, shared, and component-state-uncertain files as leftovers; table-only plans cannot prove bytes and fail closed. Unowned nested files remain, and only empty parents are pruned. Existing prefix paths are checked without following symlinks before uninstall; destination writes use no-follow directory handles and refuse case-variant conflicts. This is not a guarantee against a concurrent path-swap race. `--files-only` is CLI-only. Registry purge is separately gated on successful vendor uninstall, confirmed registration removal and no remaining mapped MSI files; a matching basename or product name alone is not proof.

## Evidence on this branch (not a release gate)

- Red/green disposable fixtures include `tests/msi_table_header_check.py`, `tests/msi_manifest_check.py`, `tests/file_plan_check.py`, `tests/plan_root_safety_check.py`, `tests/casefold_manifest_check.py`, `tests/casefold_ancestor_check.py`, `tests/casefold_prune_check.py`, `tests/component_ownership_check.py`, `tests/cross_product_ownership_check.py`, `tests/inventory_manifest_check.py`, `tests/registry_manifest_check.py`, `tests/same_size_modified_check.py`, `tests/table_only_fail_closed_check.py`, `tests/uninstall_safety_check.py`, `tests/purge_leftovers_gate_check.py`, `tests/uninstall_oserror_check.py`, `tests/apply_plan_symlink_check.py`, `tests/preset_rescue_check.py`, `tests/preset_collision_check.py` and `tests/gui_refresh_repro.py`. GUI fixtures require PySide6 on the PC.
- Metadata/listing comparisons do not establish real cabinet extraction, install, repair, uninstall, vendor custom-action, or feature-selection behavior. Keep machine-specific reports in private maintainer records.
- Synthetic destination-root fixtures cover selected unsafe mappings; they do not prove every vendor MSI folder mapping.

## Acceptance still required before release

- Run `packaging/run-suites.sh` against the exact source snapshot with a disposable HOME and Wine prefix; report passed, failed and skipped suites rather than reusing a prior count.
- Exercise the source tarball and package built from that same snapshot, including file contents and checksums.
- Complete an independent review of the final diff and reproduce every finding. A failed or incomplete delegated review is not an approval.
- Real-vendor tests, if performed, must use a disposable prefix and be recorded separately. Synthetic tests do not establish vendor custom actions, feature selection, licensing, or real-prefix behavior.
- Keep machine-specific paths, user reports, screenshots and raw logs in private maintainer records, not this public design map.

## Decision log

- Evidence beats prose: a synthetic test is not a real-vendor or published-artefact result.
- Fail closed on ambiguous destinations and modified-size files. Preserve unowned data over complete removal.
- Separate toolkit file deletion, preset rescue, Wine/vendor effects, scratch cleanup and updater effects in every safety claim.
