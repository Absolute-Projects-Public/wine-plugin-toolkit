# Changelog

All notable changes to this project. Versions that were rebuilt during a session without a
release are not listed separately - what matters is what a published version contains.

## 0.6.1

- Fixes the updater's checksum check: a release that publishes a checksum for one asset (its source
  tarball) had that checksum used to "verify" a different asset, which failed a good download and
  made `wpt update --install` refuse to install it. A sums file that names files is now read as
  such — the line for *this* asset, or no published checksum at all.
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

- **`wpt doctor`** — one command that checks everything the toolkit depends on (Wine stack, the
  required and optional external tools, plugin directories and their permissions, cached MSIs,
  the inventory, wrapper support, the scratch dir, PySide6) and says what to fix. `--json` for
  issue reports. Exit code 2 for a real problem, 1 for warnings, 0 for clean.
- **Shell completions** — `wpt completions fish|bash|zsh`, generated from the argument parser so
  they cannot go stale. The Arch package installs them.
- `wpt enable X` on an already-enabled plugin now says so and exits 0 instead of looking like a
  failure; a name that matches nothing still exits non-zero.
- Uninstalling a product whose MSI lives in Wine's installer cache reads the MSI **before**
  running msiexec (which deletes that cached copy), and stages the MSI together with its sidecar
  cabinets — without them the payload cannot be extracted at all.

## 0.5.6

- GUI: the Download Plugins and Plugins tables both have a right-click menu (open page, install
  what is already downloaded, preset sources, copy name/path/link, show in file manager).
- The download list only redraws when `~/Downloads` changes, and keeps your selected row — a
  four-second rebuild used to clear the selection mid-click.
- Matching a catalogue entry to a downloaded installer is strict: Quad Cortex is no longer
  offered the Nano Cortex installer.
- Hint lines are readable (they were styled with a colour that vanishes on dark themes).

## 0.5.5

- `wpt wrappers` identifies vendor `.exe` installers by family (NSIS, Inno Setup, InstallShield,
  WiX Burn, 7-Zip SFX, IExpress/CAB, Advanced Installer) and unpacks them on Linux where that is
  possible — Burn bundles and self-extractors included, found by content rather than by file name.
- Packages that are drivers or applications rather than plugins are refused with an explanation
  and the destination their files wanted; `--include-app-files` places them anyway.
- Preset and IR sources (`wpt presets --sources`) — community repositories, forum preset threads,
  a free vault and two shops, opened in your own browser.

## 0.5.0

- First version with the full manager surface: inventory, install, repair, uninstall, presets,
  product triage across every Wine prefix, the Neural DSP catalogue and the PySide6 GUI.
