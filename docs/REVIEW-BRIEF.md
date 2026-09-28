# Review brief — adversarial review of `wpt`

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
  it" in separate lists. If a category is clean, say so plainly — that is a result too.
- **No claim without its proof.** "This does not work" needs the failure shown.
- Do not report style opinions, formatting, or missing features.

## What to check, in order

1. **Every claim in `README.md`, against the code.** A documented command that does not work as
   written is high severity. Pay attention to the countable claims (how many commands, how many
   GUI tabs) and the absolutes (*never*, *only*, *every file*, *nothing outside the prefix*) —
   those are where documentation and code drift apart.
2. **The core modules for real defects**: `wpt/msi.py`, `installer.py`, `presets.py`, `wrappers.py`,
   `updates.py`, `scan.py`, `inventory.py`, `environment.py`, `cli.py`, `gui.py`. A behaviour that
   contradicts its own docstring counts.
3. **The suites.** Run them (`packaging/run-suites.sh`) and report the exact command and result. A
   test that cannot fail, or that asserts nothing, is itself a finding.
4. **Safety.** Anything written or deleted outside the target prefix on any code path — install,
   repair, uninstall, preset rescue, update.
5. **GUI failure modes.** Buttons left disabled, blocking dialogs, jobs that die with nothing in
   the log, two jobs racing over the same files.

## How to run things

```bash
bash packaging/run-suites.sh <tree>          # every suite, each with the environment it documents
python3 packaging/scan-identifiers.py .      # the public surface, for identifiers
```

- The GUI suites need PySide6 and must run on a machine that has it; the agent host has only the
  stdlib, so `test_core`, `test_prefix_integration`, `preset_rescue_check`, `tables_plan_check`
  and `wrapper_display_check` are the ones that run anywhere.
- `updater_e2e_check` needs a built release (`WPT_RELEASE_DIR`); it is skipped, not failed, when
  there is none.
- The suites must be run with **the environment each documents** — `updater_e2e_check` needs
  `WPT_NO_UPDATE_CHECK` *unset*, everything else with it set. `run-suites.sh` does this for you.

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
