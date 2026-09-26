# Testing `wpt`, what to run, in what order

`wpt` installs, repairs, inventories and removes **Windows audio plugins in an ableton-linux style Wine
prefix**, the shape used when a vendor's installer refuses to run under Wine. It unpacks the vendor MSI
with `msitools` and places the payload itself, then verifies every file against the MSI's own `File` table.

Nothing here is destructive unless you ask for it. Every command that changes something has `--dry-run`.

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
wpt list         # inventory: every plugin file, with its size checked against the cached MSI
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
- the output of `wpt list --json` and `wpt products --json`, machine-readable and safe to paste
- anything that crashed, with the traceback
- whether the GUI opened and every tab rendered

## Things to try, once the read-only part looks sane

```bash
wpt pending                                    # installers you have downloaded but not installed
wpt catalogue                                  # Neural DSP's download list (bundled snapshot)
wpt catalogue --refresh                        # re-read their live page
wpt catalogue --open "nolly"                   # open that plugin's download page in your browser
wpt install <some>.msi --dry-run               # the plan, without writing anything
wpt install <something>.exe --dry-run          # vendor .exe: see how it would get to an MSI
wpt disable Nolly --dry-run                    # hide a plugin from the DAW scanner (reversible rename)
wpt uninstall --product <X> --dry-run --purge  # exactly what would be removed, files and registry
```

Read `wpt uninstall --dry-run` output carefully before ever running it for real: it lists every file the
MSI placed, and `--purge` adds the registry entries that point at them.

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
| `ok` | the file on disk is exactly the size the MSI's `File` table promised |
| `unverified` | no MSI that describes this file was found, so there is nothing to compare it with, `wpt find-msi` shows which MSIs it can see |
| (not listed) | executables in `Program Files` that no plugin MSI describes. Wine's own tools and other vendors' helpers. Counted in one line; `wpt list --all-standalone` lists them |
| `BROKEN` | the size disagrees with the MSI: usually a half-written install, and `wpt repair` re-places just that file |

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

- **Presets**: `uninstall` copies your own presets and any downloaded packs to
  `~/.local/share/wpt/presets/<product>/<timestamp>/` before it removes anything. `--no-rescue` turns that
  off. Do not use it on work you care about.
- **Activations**: deleting files does not free an iLok/PACE activation. Deactivate in iLok License
  Manager first, or use *Report as Unusable* there if the location is unreachable.
- **Uninstall routes through `msiexec /x` first**, which is a no-op on prefixes where the product was
  never registered with Windows Installer. That is expected, not a bug: the file-level step does the work.
  It also deletes Windows Installer's cached copy of the MSI, which is why the toolkit reads everything it
  needs from that MSI *before* running it, and why a product uninstalled that way can have no MSI left
  afterwards. If you then ask the toolkit to remove it again it will tell you what the prefix still holds
  under that name instead.
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
- `uninstall --purge` removes the product's own registration entries and stale pointers to its files; it
  leaves unrelated vendor cache keys alone.
- `scan`'s "plugins present" list can include plugin support dlls (Qt's `qwindows.dll` lives in plugin
  directories), legitimate entries, just more than the four paths you may be looking for.

## Tests, if you want to run them

```bash
python3 tests/test_core.py                              # pure logic, no prefix needed
python3 tests/test_prefix_integration.py                # builds a synthetic prefix, exercises everything
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py    # builds the GUI and runs every tab (needs PySide6)
QT_QPA_PLATFORM=offscreen python3 tests/gui_downloads_check.py   # Download tab behaviours
QT_QPA_PLATFORM=offscreen python3 tests/gui_update_check.py      # the update check, with the network and the dialogs stubbed
QT_QPA_PLATFORM=offscreen python3 tests/gui_job_decline_check.py # declined jobs must not leave dead buttons
```

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
