# Review brief: adversarial review of `wpt`

Hand this to a reviewer: a person, or an agent with **no context** from the session that wrote the
code. It exists because the round whose brief said *"check every claim in the README against what
actually happens"* found defects that rounds without it did not.

## Rules for the reviewer

- **Read-only.** No edits, no commits, no pushes, no `sudo`, no package installs, nothing written
  outside a temp directory.
- **Never** run `wpt install / uninstall / repair / disable / enable` against a real prefix.
  `--dry-run` is fine, and so is the synthetic prefix `tests/test_prefix_integration.py` builds.
- **Evidence or nothing.** Every finding carries the exact command you ran and its **real** output,
  quoted rather than paraphrased. Keep "reproduced" and "looks suspicious but I did not reproduce
  it" in separate lists. If a category is clean, say so plainly: that is a result too.
- **No claim without its proof.** "This does not work" needs the failure shown.
- Do not report style opinions, formatting, or missing features.

## What to check, in order

1. **Every claim in `README.md`, against the code.** A documented command that does not work as
   written is high severity. Pay attention to the countable claims (how many commands, how many
   GUI tabs) and the absolutes (*never*, *only*, *every file*, *nothing outside the prefix*):
   those are where documentation and code drift apart.
2. **The core modules for real defects**: `wpt/msi.py`, `installer.py`, `presets.py`, `wrappers.py`,
   `updates.py`, `scan.py`, `inventory.py`, `environment.py`, `cli.py`, `gui.py`. A behaviour that
   contradicts its own docstring counts.
3. **The suites.** Run them (`packaging/run-suites.sh`) and report the exact command and result. A
   test that cannot fail, or that asserts nothing, is itself a finding.
4. **Safety.** Anything written or deleted outside the target prefix on any code path, including install,
   repair, uninstall, preset rescue, update.
5. **GUI failure modes.** Buttons left disabled, blocking dialogs, jobs that die with nothing in
   the log, two jobs racing over the same files.

## How to run things

```bash
bash packaging/run-suites.sh .             # every suite, each with the environment it documents
python3 packaging/scan-identifiers.py . --assets "${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}" # tracked repo, source tarballs, release notes
```

- The GUI suites need PySide6 and must run on a machine that has it. A host without PySide6 can run
  `test_core`, `test_core_home_safety_check`, `test_prefix_integration`, `doctor_override_check`,
  `preset_rescue_check`, `tables_plan_check` and `wrapper_display_check`.
- `updater_e2e_check` needs an extracted source tree in `WPT_RELEASE_DIR` and all four current-version
  assets in `WPT_RELEASE_ASSET_DIR` (default: `WPT_RELEASE_DIR/dist`). The runner verifies both
  `.sha256` sidecars before the test. With incomplete or old assets, it reports an explicit skip by
  default. Set `WPT_REQUIRE_UPDATER_E2E=1` to fail on missing assets; invalid sidecars always fail.
- The suites must be run with **the environment each documents**. `updater_e2e_check` needs
  `WPT_NO_UPDATE_CHECK` *unset*, everything else with it set. `run-suites.sh` does this for you.
- For the release gate, point the runner at the extracted source and the built-asset archive:

  ```bash
  export WPT_RELEASE_DIR=/path/to/extracted-candidate
  export WPT_RELEASE_ASSET_DIR="${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"
  export WPT_REQUIRE_UPDATER_E2E=1
  bash "$WPT_RELEASE_DIR/packaging/run-suites.sh" "$WPT_RELEASE_DIR"
  ```

  Run this on an Arch-family host so the real CLI install-display checks use the supported package path.
  Relative paths and `TMPDIR` are anchored to the caller's working directory; the runner prints a unique
  log directory for each invocation and removes its separate scratch directory on exit.

## What a good report looks like

```
FINDING 3 (high, reproduced)
  README:24 "Verifies every file it placed against the MSI's own File table, size for size."
  Code:   installer.py:401-404 matches on action.dest.name, and msi.py:202 keys expected sizes by
          base name, so two files sharing a name collapse to one.
  Ran:    python3 - <<'PY' … (the 12-line reproduction)
  Output: expected_sizes() -> {'Plugin.ico': 999999}
          verify_plan on the 4096-byte file: [('size-mismatch', 'on disk 4096, MSI says 999999')]
  Effect: a correctly installed file reports BROKEN, and bundle-internal files are never checked.
```
