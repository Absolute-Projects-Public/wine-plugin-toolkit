# Changelog

All notable changes to this project. Versions that were rebuilt during a session without a
release are not listed separately - what matters is what a published version contains.

## Unreleased

Stability fixes from a GUI review, each one reproduced before it was fixed and pinned in
`tests/gui_job_failure_check.py`.

- **A failed uninstall pre-check now says so.** The worker that reads the MSI before the
  confirmation dialog was the one action with no failure handler, so an MSI msitools could not
  read left *Uninstall…* disabled with nothing in the log to explain it. It reports the error and
  hands the button back, like every other action.
- **The change-detected downloads scan can no longer run on the GUI thread.** When a scan was
  already in flight, `refresh_downloads(background=True)` fell through to the synchronous
  msitools scan instead of returning, freezing the window - and it does exactly that when a
  download finishes mid-scan, which is when it is called.
- **"Find in prefix" reads the MSIs on a worker.** It runs one `msiinfo` read per cached MSI,
  which is seconds of work on a busy prefix, and it was doing it on the GUI thread. The combo is
  also only replaced once the scan succeeds, so a failed scan no longer discards the installer
  you had already picked.
- **Enable/Disable is declined while another job holds the prefix.** The rename happens on the
  GUI thread and the uninstall path deletes both the file and its `.disabled` name, so the two
  raced over the same files. It now follows the same one-job-at-a-time rule as everything else.
- **Empty tables stay explained.** Blanking the plugin table for a screenshot, or clearing the
  install table for a second preview, left a large empty grid on screen where the note belongs.
- **The preset-source note is readable and populated.** It carried an inline
  `color: palette(mid)`, which is a *background* role: on a dark desktop it rendered at 1.25:1,
  and it was resolved once so the dark switch never re-coloured it either. It also stayed blank
  until the preset-source dropdown was changed, because the signal is connected after the combo
  is filled.

## 0.6.3

Presentation: the repository now reads like something a newcomer can follow.

- The README is reorganised: what it does, why it exists, requirements, then **Install**, then how to use
  it. It opens with the icon and release badges instead of a paragraph.
- New **Coming from Windows** section: moving your installers, your own presets and your licences across,
  and an honest list of what will not work here (drivers, Linux-native plugins).
- A **troubleshooting** table for the failures people actually hit, and the install instructions no longer
  assume you already build Arch packages.
- Em dashes are gone from the docs and from the interface text.

GUI pass: the workflow is now left-to-right, and the tabs say what they do.

- **Tab order and names**: Environment · Plugins · **Download** · **Pending Install** · **Install MSI** ·
  Diagnostics. Downloading and installing are two steps, so they are two tabs. *Install downloaded
  installer* and the *watch ~/Downloads* switch are gone from the browse tab, and every install now
  goes through Pending Install. (The change-detected refresh stayed: it no longer needs a switch.)
- ***Open in browser* sits next to the dropdown it acts on** in the *Preset & IR sources* row, instead
  of at the far right of the window with nothing tying it to the label.
- **A tab with nothing in it explains itself** instead of showing a large empty grid: Pending Install,
  Install MSI and Diagnostics say what to press, and the table appears the moment it has rows.
- **Columns keep a minimum width**, so a table with no rows no longer collapses Status to 40 px and
  Kind to 35 px.
- **The five "What to install" checkboxes are grouped**, not spread across the whole window.
- **Dark mode is a header switch** (and a line in Settings). Off by default, the window follows your
  desktop theme, which is what makes it look native; the switch is for the other case, and it applies
  immediately without a restart. The choice is remembered in `~/.config/wpt/config.json`.
- **Settings & about** opens a dialog with the version, every path the toolkit resolved and the config
  file, what a bug report needs. The update-check toggle lives there too.
- **The uninstall confirmation no longer reads the MSI on the GUI thread**, the last of the two
  accepted limitations from the review round. The dialog says the same thing; the window cannot freeze
  on the way to it.
- `tests/render_tabs.py` renders any tab at any size, reads the tab labels from the window (a
  hardcoded list was already naming the wrong tab), and its `neutral` mode blanks before every grab, so
  a published image cannot show which plugins the machine that rendered it happens to have.
- `WPT_SCREENSHOT_MODE=1` (+ `WPT_SCREENSHOT_TAB`, `WPT_SCREENSHOT_DARK`) takes a publishable capture
  on a real desktop; `tests/gui_layout_check.py` covers the empty states, the column floors and the
  dark switch.

## 0.6.2

- Test-only: the GUI update-check suite asserted a hardcoded version, so running the suites from the
  0.6.1 source tarball reported one failure that had nothing to do with the code. It reads the
  running version now.

## 0.6.1

- Fixes the updater's checksum check: a release that publishes a checksum for one asset (its source
  tarball) had that checksum used to "verify" a different asset, which failed a good download and
  made `wpt update --install` refuse to install it. A sums file that names files is now read as
  such, the line for *this* asset, or no published checksum at all.
- Every release attaches a checksum per asset, so the updater can verify the package it installs.

## 0.6.0 - first public release

- **`wpt update`** and the GUI's update check: asks GitHub for the newest release (once a day,
  cached), compares it with the running version, and offers to download and install it. The
  download is verified against the release's sha256 when published, and refused unless it is really
  a pacman package. Installing happens in a visible terminal running `sudo pacman -U …`; the app
  closes first and reopens afterwards. `WPT_NO_UPDATE_CHECK=1` disables the check.
- **`wpt doctor`** - one command that checks everything the toolkit depends on and says what to fix;
  `--json` for issue reports. Exit code 2 for a real problem, 1 for warnings, 0 for clean.
- **Shell completions** - `wpt completions fish|bash|zsh`, generated from the argument parser. The
  Arch package installs all three.
- `wpt enable X` on an already-enabled plugin says so and exits 0; a name matching nothing still
  exits non-zero.
- Uninstalling reads the MSI **before** running msiexec (which deletes Wine's cached copy) and
  stages it together with its sidecar cabinets.
- MIT licence, a changelog, and no machine-specific defaults: the Wine user is detected from the
  prefix rather than assumed.

## 0.5.8

- **`wpt doctor`**, one command that checks everything the toolkit depends on (Wine stack, the
  required and optional external tools, plugin directories and their permissions, cached MSIs,
  the inventory, wrapper support, the scratch dir, PySide6) and says what to fix. `--json` for
  issue reports. Exit code 2 for a real problem, 1 for warnings, 0 for clean.
- **Shell completions**, `wpt completions fish|bash|zsh`, generated from the argument parser so
  they cannot go stale. The Arch package installs them.
- `wpt enable X` on an already-enabled plugin now says so and exits 0 instead of looking like a
  failure; a name that matches nothing still exits non-zero.
- Uninstalling a product whose MSI lives in Wine's installer cache reads the MSI **before**
  running msiexec (which deletes that cached copy), and stages the MSI together with its sidecar
  cabinets, without them the payload cannot be extracted at all.

## 0.5.6

- GUI: the Download Plugins and Plugins tables both have a right-click menu (open page, install
  what is already downloaded, preset sources, copy name/path/link, show in file manager).
- The download list only redraws when `~/Downloads` changes, and keeps your selected row, a
  four-second rebuild used to clear the selection mid-click.
- Matching a catalogue entry to a downloaded installer is strict: Quad Cortex is no longer
  offered the Nano Cortex installer.
- Hint lines are readable (they were styled with a colour that vanishes on dark themes).

## 0.5.5

- `wpt wrappers` identifies vendor `.exe` installers by family (NSIS, Inno Setup, InstallShield,
  WiX Burn, 7-Zip SFX, IExpress/CAB, Advanced Installer) and unpacks them on Linux where that is
  possible. Burn bundles and self-extractors included, found by content rather than by file name.
- Packages that are drivers or applications rather than plugins are refused with an explanation
  and the destination their files wanted; `--include-app-files` places them anyway.
- Preset and IR sources (`wpt presets --sources`), community repositories, forum preset threads,
  a free vault and two shops, opened in your own browser.

## 0.5.0

- First version with the full manager surface: inventory, install, repair, uninstall, presets,
  product triage across every Wine prefix, the Neural DSP catalogue and the PySide6 GUI.
