# Testing `wpt`, what to run, in what order

`wpt` installs, repairs, inventories and removes **Windows audio plugins in an ableton-linux style Wine
prefix**, the shape used when a vendor's installer refuses to run under Wine. It unpacks the vendor MSI
with `msitools` and places the payload itself. Full-path MSI rows cover duplicate basenames and bundle
internals. Inventory and repair remain size-based; `ok` is not a content hash or proof a component was
selected. Install plans reject unsupported component conditions/attributes, and direct removal preserves
component-state-uncertain files.
See [the claim ledger](docs/CLAIMS.md).

Install, uninstall and Wine wrapper paths can change files. Use `--dry-run` where offered and a disposable
prefix for tests. A dry-run flag is not by itself proof that an external vendor program has no side effects.

## What you need

- Linux, Python 3.11+ (3.14 works)
- `msitools`, `sudo pacman -S msitools` (Arch/CachyOS) · `sudo apt install msitools` (Debian/Ubuntu)
- A Wine prefix of the ableton-linux shape: `~/.wine-ableton` plus a staged
  `~/.local/opt/wine-d2d1-nspa-<version>` tree. Point it elsewhere with `--prefix` / `--tree`.
- `pyside6` only if you want the GUI (`sudo pacman -S pyside6`)

## Install

Arch/CachyOS:

```bash
sudo pacman -U wine-plugin-toolkit-*-1-any.pkg.tar.zst
wpt --version
```

Anything else, from the source tarball:

```bash
tar xzf wpt-*.tar.gz && cd wine-plugin-toolkit-*
pip install .              # console script `wpt`
pip install '.[gui]'       # adds `wpt-gui`
```

## The five-minute smoke test

All of these are read-only. Run them and keep the output:

```bash
wpt env          # does it find your Wine tree, prefix, Windows user and plugin directories?
wpt list         # inventory: full-path sizes checked against cached MSIs, not file hashes
wpt scan         # registry vs disk: installs that registered but never copied their files
wpt products     # triage of every Wine prefix on the machine, newest install first
wpt presets      # your presets and downloaded packs, per product
wpt-gui          # the GUI: Environment, Plugins, Download, Pending Install, Install MSI, Diagnostics
```

Worth knowing what "good" looks like: `env` resolves every path, `list` shows sizes matching the MSI
(`ok`), `scan` reports `0 MISSING` on a healthy prefix. If `scan` reports missing paths, that is the exact
failure this tool exists for, send that output.

## What to report back

- your distro, Python and Wine-tree version (`wpt env` shows both, plus the toolkit version)
- the output of `wpt list --json` and `wpt products --json` **after** redacting usernames,
  home paths and private details; JSON can contain local paths
- anything that crashed, with the traceback
- whether the GUI opened and every tab rendered

## Things to try, once the read-only part looks sane

```bash
wpt pending                                    # installers you have downloaded but not installed
wpt catalogue                                  # Neural DSP's download list (bundled snapshot)
wpt catalogue --refresh                        # re-read their live page
wpt catalogue --open "nolly"                   # open that plugin's download page in your browser
wpt install <some>.msi --dry-run               # preview prefix changes; scratch may be written
wpt install <something>.exe --dry-run          # vendor .exe: see how it would get to an MSI
wpt disable Nolly --dry-run                    # hide a plugin from the DAW scanner (reversible rename)
wpt uninstall --product <X> --dry-run --purge  # mapped File-table files/registry, not Wine effects
```

Read `wpt uninstall --dry-run` output carefully before ever running it for real: it shows eligible
mapped File-table files and known refusal reasons, not every user file Wine might remove or every
possible vendor side effect. A registered uninstall may stop before `msiexec` when the MSI payload
bytes are unavailable, a cached ownership scan is incomplete, or another product claims a planned path.
`--purge` also previews the registry entries it would target.

## The `.exe` question

Vendors ship a `.exe`; this tool needs the `.msi` inside it. `wpt install` takes either and bridges the gap
by the cheapest route: use the MSI as-is → use the MSI already cached in the prefix → unpack it with the
tool that suits the wrapper's own format → *or*, with `--run-wrapper` (or the GUI's confirmation), run the
vendor installer under Wine once and pick up what it caches. Neural DSP's wrappers are Advanced Installer
LZMA packages that no Linux tool can unpack, so for those the Wine step is the one that works, the tool
tells you which route it took instead of failing silently.

You can ask it what it makes of a file before committing to anything, and nothing is written outside a
scratch directory:

```bash
wpt wrappers                                    # every .exe installer around, its family, and its route
wpt wrappers --file ~/Downloads/Thing.exe       # one file in detail
wpt wrappers --file ~/Downloads/Thing.exe --unpack   # try the Linux route and report what came out
wpt wrappers --json                             # machine-readable
```

It recognises NSIS, Inno Setup, InstallShield, WiX Burn bundles, 7-Zip and CAB/IExpress self-extractors,
and Advanced Installer, and for an unrecognised file it falls back to trying every unpacker you have
installed. Where a wrapper carries several MSIs (Windows runtime bundles do), it names the one it would use
and lists the others rather than picking silently.

The **Download** tab is the front end for all of it: their catalogue with versions and release dates,
whether each plugin is installed in your prefix, whether an installer is already in `~/Downloads`, and
*Download Selected Plugin* (their plugin links need you signed in, so that step is yours). Where the file
is installed from is the **Pending Install** tab, download here, install there, and the tab order follows
that flow: Environment, Plugins, Download, Pending Install, Install MSI, Diagnostics.

## Getting more presets

```bash
wpt presets --sources                            # community repositories, vaults, forum threads, shops
wpt presets --open "Preset Junkie"               # opens in your browser (nothing is downloaded for you)
wpt presets --open "forum" --product "Nolly"     # where a site supports it, opens already searching
```

Preset Junkie, the Neural DSP forum's preset threads, Honest Amp Sims' Preset Vault, r/NeuralDSP and two
pack shops. The toolkit opens the page in your own browser, several of them need a sign-in, which is
exactly why it does not try to fetch anything itself.

## What the verdicts mean

| verdict | meaning |
|---|---|
| `ok` | this full file path's byte size matches one unambiguous MSI File row; content hashes are not checked |
| `unverified` | no unique mapped MSI owner/size; `wpt find-msi` shows which MSIs it can see |
| (not listed) | executables in `Program Files` that no plugin MSI describes. Wine's own tools and other vendors' helpers. Counted in one line; `wpt list --all-standalone` lists them |
| `BROKEN` | this full path's byte size disagrees; repair may preserve an existing no-clobber preset |

`unverified` is not a failure. It means the toolkit has no reference for the file, commonly because the
product was installed by hand, or by a wrapper that kept its MSI somewhere unusual.

## "It installed but my DAW can't see it"

Check what the package actually contains before assuming a fault:

```bash
wpt inspect <the msi>        # or: wpt install <the download> --dry-run
```

If the plan says *no VST3/VST2/AAX payload*, the download is an application or a driver rather than a
plugin, some vendor downloads are exactly that (Neural DSP's Nano Cortex is a Windows USB driver plus a
control panel). It is refused by default so nothing lands in the wrong place; `--include-app-files` places
its files anyway, but a DAW will still not scan them. A driver also wants its INF and service registration,
which only its own installer performs.

Another thing worth knowing: **the "In the prefix" column reads the disk as well as the MSIs**, so a plugin
the toolkit placed itself (which leaves no MSI behind) still shows as installed, and an app/driver shows as
*files only* once its files exist.

## Cautions

- **Presets and user files**: both CLI and GUI rescue before `msiexec /x`. WPT strictly enumerates and
  attempts to copy regular files under planned no-clobber paths and recognized Roaming/MIDI XML maps;
  unreadable/incomplete traversals, detected symlinks, unsupported file types or copy failures stop
  removal. Candidate-name lookup and uninspected RemoveFile/custom actions do not prove complete discovery.
  Direct WPT removal preserves no-clobber files in place, changed files, shared paths, and components
  whose installed state cannot be proved. Other vendor-created files may not be recognised, and Wine
  can still affect them or host-mapped paths; back up first. The
  GUI has no `--files-only` option; on the CLI it skips Wine's `msiexec` step. `--no-rescue` skips that
  CLI backup.
- **Activations**: deleting files does not free an iLok/PACE activation. Deactivate in iLok License
  Manager first, or use *Report as Unusable* there if the location is unreachable.
- **Registered uninstall**: WPT reads the MSI before `msiexec /x`, which may remove Windows Installer's
  cached copy. The default registered route stops before vendor removal when payload bytes or cached
  ownership cannot be verified, the selected MSI has an unmapped root, fallback Manufacturer/ProductName
  path components are unsafe, or another cached product claims a planned path. After a zero `msiexec`
  exit it also requires registration to be gone and readable before direct removal or purge.
  Unregistered products skip `msiexec`; the file-only path removes only eligible payload-backed files.
- **Dry-run scope**: `uninstall --dry-run` still stages the MSI and extracts into scratch. It shows mapped
  file actions and known refusal reasons, not every external effect of a real Wine uninstall. A directory
  destination can contain files beyond the plan; back those up first. The explicit `--no-files` option
  bypasses WPT's plan, rescue, and shared-file ownership checks but still invokes Wine when registered;
  `msiexec /x` can remove files another product claims.
- Scope for this version: **only** the ableton-linux style prefix. Steam/Lutris/Bottles prefixes are
  triaged (read-only in `wpt products`) but never written to.

## Known limits

- Wrapper support covers the families listed above, but each needs the right tool installed (`7zip`,
  `cabextract`, `innoextract`; `unshield` for InstallShield). What is missing is reported, not guessed at.
- **Inno Setup is version-limited by `innoextract`**: a current innoextract refuses a brand-new setup-data
  version (6.3.0 seen) and says so. The newest Inno installers therefore need Wine even though the family
  is identified correctly.
- A wrapper whose payload is a **bare plugin tree** rather than an MSI (no `VST3DIR`/`VSTDIR` layout either)
  cannot be installed by this tool: it installs from MSIs, and it says so rather than copying files it
  cannot verify against a `File` table.
- MSI rows from source-only, optional, conditioned, shared-reference, permanent, transitive, `Shared`
  or `NeverOverwrite` components are not treated as proof of installed ownership. Install planning
  refuses unsupported component semantics; direct uninstall preserves those paths as leftovers.
- Cross-product ownership includes mapped application/driver roots and can inspect non-plugin MSI
  records in Windows Installer's cache. An unreadable or unmapped cached MSI blocks registered
  removal conservatively; a failed `msiexec` also prevents direct file removal and registry purge.
- Preset rescue prefers the selected MSI's Manufacturer for its ProgramData tree. Existing files in a
  no-clobber destination without a successful rescue result stop the uninstall before `msiexec`.
- `uninstall --purge` removes the product's own registration entries and stale pointers to its files; it
  leaves unrelated vendor cache keys alone.
- `scan`'s "plugins present" list can include plugin support dlls (Qt's `qwindows.dll` lives in plugin
  directories), legitimate entries, just more than the four paths you may be looking for.

## Tests, if you want to run them

```bash
python3 tests/test_core.py                              # pure logic, no prefix needed
python3 tests/test_prefix_integration.py                # builds a synthetic prefix, exercises everything
python3 tests/readme_claims_check.py                    # mechanical README claims, not an MSI proof
python3 tests/scanner_check.py                          # filenames with spaces / missing assets fail closed
python3 tests/tarball_source_check.py                   # reproducible archive / dirty, version, pin and tag guards
python3 tests/version_consistency_check.py              # runtime, project, package and changelog versions agree
python3 tests/package_data_check.py                     # wheel configuration includes catalogue and icons
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py    # every tab, fixture-backed catalogue refresh, clean close
QT_QPA_PLATFORM=offscreen python3 tests/gui_downloads_check.py   # Download tab behaviours
QT_QPA_PLATFORM=offscreen python3 tests/gui_update_check.py      # the update check, with the network and the dialogs stubbed
QT_QPA_PLATFORM=offscreen python3 tests/gui_env_redetect_guard_check.py # Re-detect cannot switch prefixes while a worker is retained
QT_QPA_PLATFORM=offscreen python3 tests/gui_job_decline_check.py # declined jobs must not leave dead buttons
QT_QPA_PLATFORM=offscreen python3 tests/gui_job_failure_check.py # a job that dies must not leave a dead button either
QT_QPA_PLATFORM=offscreen python3 tests/gui_refresh_repro.py     # overlapping refresh is declined without losing the first
QT_QPA_PLATFORM=offscreen python3 tests/gui_startup_close_check.py # delayed catalogue callback must not run after close
python3 tests/standalone_launch_check.py                          # unique match, prefix containment, custom Wine env; Popen mocked
QT_QPA_PLATFORM=offscreen python3 tests/gui_plugin_context_menu_check.py # menu labels/availability; launch is mocked
python3 tests/launch_profiles_check.py                             # profile schema, private/concurrent persistence, literal env parsing; temp home/prefix
QT_QPA_PLATFORM=offscreen python3 tests/gui_launch_profiles_check.py # profile switching, stale-view invalidation, modal race, rename, standalone propagation; temp home/prefix
python3 tests/preset_rescue_check.py                            # the preset rescue, and the removal/staging edge cases
python3 tests/preset_collision_check.py                         # changed preset in the same minute cannot overwrite a rescue
python3 tests/uninstall_safety_check.py                         # CLI gates, unmapped roots, registration and rescue ordering
python3 tests/uninstall_race_safety_check.py                    # parent symlink swap cannot redirect File-table unlink
QT_QPA_PLATFORM=offscreen python3 tests/gui_uninstall_safety_check.py # GUI gates/rescue ordering; requires PySide6
python3 tests/cross_product_ownership_check.py                  # shared/ambiguous cached MSI owners fail closed
python3 tests/no_clobber_provenance_check.py                    # no-clobber provenance, repair preservation and rescue (11/11)
python3 tests/component_ownership_check.py                      # component conditions/flags preserve uncertain files
python3 tests/tables_plan_check.py                              # table-only removal preserves unverified payloads
python3 tests/msi_table_header_check.py                         # msiinfo third metadata line is not a data row
python3 tests/msi_manifest_check.py                             # Directory/Component/File mapping and basename collisions
python3 tests/file_plan_check.py                                # exact paths, mixed-case roots, repair and payload refusal
python3 tests/plan_root_safety_check.py                         # root traversal, unmapped TARGETDIR and Program Files mapping
python3 tests/unowned_directory_check.py                        # unowned/modified files survive a planned bundle removal
python3 tests/casefold_manifest_check.py                        # case variant and ambiguous case collision
python3 tests/inventory_manifest_check.py                       # same basename in two MSIs has separate owner/size
python3 tests/registry_manifest_check.py                        # purge cannot take an unowned user's registry pointer
python3 tests/wrapper_display_check.py                          # a wrapper whose window could not appear is refused, not launched
WPT_RELEASE_DIR=~/wpt-release python3 tests/updater_e2e_check.py # the updater against built release artefacts
```

Three of the checks that matter most now ship with the project instead of living in an agent's
workspace, because a reviewer should be able to run them too:

```bash
bash packaging/run-suites.sh .             # every suite, each with the environment it documents
python3 packaging/scan-identifiers.py . --assets ./dist # tracked repo + local tarballs + release bodies
python3 packaging/redact-releases.py --dry-run   # redact the notes of releases older than the current one
```

`scan-identifiers.py` marks tarballs **SKIPPED** unless `--assets` points at a directory with at least
one source tarball; it fails closed on unreadable or oversized members. A clean result is only for
the surfaces actually scanned, and the current 0.6.4 release body contains an allowed quoted error
that still appears as a finding for review.

`run-suites.sh` exists because handing every suite the same environment produces a false failure:
`updater_e2e_check` drives the real CLI, so it must run with `WPT_NO_UPDATE_CHECK` **unset**, and it
is skipped rather than failed when there is no built release to test against. `scan-identifiers.py`
checks three places, only one of which a `.gitignore` covers — the tracked files, tarballs supplied
through `--assets`, and GitHub release *bodies* fetched from the API, which are published prose that
no scrub reaches.

`tests/gui_job_failure_check.py` covers the other half of the same plumbing: a job that *fails*. It
pins the six defects a review reproduced on 2026-09-27 - an uninstall pre-check whose worker raised
(silently, with its button left disabled), a background downloads scan that fell through to the
synchronous msitools walk on the GUI thread, the empty-state note going missing when a table was
emptied, `find_msis` reading the prefix on the GUI thread, Enable/Disable racing a running job, and
the preset-source note being both unreadable on a dark desktop and blank until the dropdown was
touched. Each fix has a check here, so a regression fails the suite rather than a screenshot.

`tests/gui_startup_close_check.py` closes a window immediately after showing it, then runs Qt's event
loop past the startup timer deadline while intercepting the delayed catalogue callback. It fails if the
callback reaches the closed window, without allowing a network request or background scan to start.
It also fails quickly if the disposable HOME has no detected Wine environment, rather than hanging on
a modal warning. `gui_env_redetect_guard_check.py` ensures the selected environment cannot change while
background work is retained; the job closures also capture that environment before starting.

`tests/standalone_launch_check.py` verifies exact/near-miss names, case-only ambiguity, disabled and
non-plugin entries, outside-prefix and symlinked leaf/ancestor paths, `..` refusal, MSI-owner mismatch,
fail-closed launch-size changes, custom-Wine environment sanitization, absolute and fallback XDG log
placement, failed-Popen log cleanup, missing runtime, output capture and asynchronous reaping. Process
creation is mocked. `tests/gui_plugin_context_menu_check.py` drives an offscreen PySide window and checks
visible labels/warnings, second-launch blocking, process-group success/error paths, Escape-safe and
re-entrant close handling, update-restart blocking, prefix-write guards on install, repair, uninstall,
enable/disable and pending wrappers, allowed dry-run preview, launch errors, mocked Browse routing, and
disposal of both context menus. Neither test starts a vendor application or reaches iLok.

For a **live desktop smoke test**, stage the review tree under an isolated PC scratch directory and use a
separate HOME/XDG set plus a disposable `WINEPREFIX` containing only inert fixtures. Verify the Environment
tab resolves the scratch paths, use Plugins → Refresh inventory, close the window normally, then verify the
exact PID exited and no `python3 -m wpt.gui` process remains. This checks render/refresh/close plumbing only;
it is not proof of MSI ownership or real-prefix safety. Do not run `wpt-gui-on-desktop.sh` unchanged for this
test: it kills an existing `python3 -m wpt.gui` process and launches the installed `~/wpt-test` tree.

For remote Wayland tests that open URLs or folders, set a short private `TMPDIR` and explicitly set
`XDG_SESSION_TYPE=wayland`. A deeply nested scratch `TMPDIR` can make Chromium's Unix-domain socket path
too long, while an SSH-provided `tty` session type can prevent Nautilus from connecting.

Do not probe the GUI entry point with `python3 -m wpt.gui --help` piped to `head`: the GUI event loop does
not exit as a help command, so the pipeline can strand a detached Qt process. Use `python3 -c 'import wpt.gui'`
for a bounded import check, or run the purpose-built GUI test suite.

Historical interactive trials are not a substitute for current automated gates and are not summarized
here. Keep machine-specific logs, real-prefix observations, screenshots, and user reports in a private
maintainer record. For release evidence, rerun the exact source snapshot on a disposable prefix and record
its revision, command, exit status, skip reasons, and local log location; do not carry over a historical
suite count.

`tests/updater_e2e_check.py` is the release gate for `wpt update`: it stands a GitHub-API-shaped
stub on localhost, serves the four assets from `dist/`, and then runs the real updater code -
`latest_release`, the version comparison, asset selection, the download, the zstd check and the
per-asset checksum - plus `wpt update --install show` exactly as a user on the previous release
would run it. It needs a built release (`WPT_RELEASE_DIR=~/wpt-release`) and exits 2 rather than
failing when the artefacts are not there yet.

`tests/gui_buttons_check.py` clicks every enabled button in every tab offscreen and fails on any exception.
The install/uninstall/repair buttons are clicked too, with `apply_plan`, `uninstall_product` and
`purge_registry` replaced by recording stubs, so the handlers run for real (that is where the wiring
bugs live) while nothing can reach the prefix. Only buttons that open a modal dialog are skipped. 
it exists because a `clicked` signal once handed a `checked` bool to a slot that took a release and crashed.
`tests/render_tabs.py` renders tabs to PNG so you can *look* at the layout instead of describing it.

```bash
QT_QPA_PLATFORM=offscreen python3 tests/gui_buttons_check.py
QT_QPA_PLATFORM=offscreen python3 tests/render_tabs.py /tmp   # writes /tmp/tab-*.png
```

`tests/gui_job_decline_check.py` covers the job plumbing: a second action while another job is running
must explain the decline **and** leave its buttons and status line usable (they used to stay disabled,
claiming work that never started), and the install job must receive its four options as plain values
read on the GUI thread rather than calling `isChecked()` from the worker thread.

`tests/gui_update_check.py` covers the update flow without touching the network: the startup check is
suppressed by `WPT_NO_UPDATE_CHECK=1`, a newer release is announced, an older one is not, a failed check
is reported rather than swallowed, a stub "package" is fetched and validated, one that is not a pacman
package is refused before anything is handed over, and "skip this version" is remembered while a manual
check still shows it.

`tests/gui_downloads_check.py` covers the Download tab: an idle watch tick must not change the rows
or the selection, a refresh must keep the row you selected, *Download Selected Plugin* must say so when
nothing is selected, and the right-click menu must offer the actions it claims.

```bash
QT_QPA_PLATFORM=offscreen python3 tests/gui_downloads_check.py
```

`tests/gui_refresh_repro.py` is the one for a GUI crash: it starts a refresh, immediately starts another
(the sequence that killed the window on 0.5.0), and requires the second to be declined, the first to deliver
rows, and the process to exit cleanly.

```bash
QT_QPA_PLATFORM=offscreen python3 tests/gui_refresh_repro.py
```

`tests/wrapper_corpus.py` is the one for a real machine, it identifies every installer found in
`~/Downloads`, `~/.cache/winetricks` and the prefix root, and for each one that claims a Linux route it
actually unpacks it and checks the MSI that comes out with `msiinfo`:

```bash
python3 tests/wrapper_corpus.py                  # ~/Downloads, winetricks cache, prefix root
python3 tests/wrapper_corpus.py some.exe         # exactly these files
python3 tests/wrapper_corpus.py --no-unpack      # identify only, run nothing
```

It exits non-zero if a wrapper is unrecognised or claims a route that yields nothing, so a new vendor
installer is a one-command check.
