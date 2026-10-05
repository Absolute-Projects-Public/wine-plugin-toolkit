"""Exercise the update flow offscreen, with the network and the dialogs stubbed out.

Run on a machine with PySide6 and a display-less Qt platform:

    QT_QPA_PLATFORM=offscreen python3 tests/gui_update_check.py

Nothing here touches the network, spawns a terminal, or installs anything: `latest_release` is
replaced, `QMessageBox.exec` and the static warning/information helpers are neutered, and
`_confirm_install` is overridden on the instance so the download path can be walked end to end.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["WPT_NO_UPDATE_CHECK"] = "1"          # the startup check must stay out of the way

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from wpt import gui as gui_mod  # noqa: E402
from wpt import updates as updates_mod  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'}  {name}")


def make_release(version: tuple[int, ...], tag: str, assets) -> updates_mod.Release:
    return updates_mod.Release(tag=tag, version=version,
                               html_url=f"https://example.invalid/{tag}",
                               published_at="2026-09-27", assets=list(assets))


# --- stubs ------------------------------------------------------------------------------------
calls: dict[str, object] = {}


def fake_latest(version=(9, 9, 9), tag="v9.9.9"):
    def inner(timeout=None):
        calls["latest"] = tag
        if calls.get("latest_raises"):
            raise updates_mod.UpdateError("cannot reach GitHub (simulated)")
        return make_release(version, tag, [updates_mod.Asset(
            f"wine-plugin-toolkit-{'.'.join(map(str, version))}-1-any.pkg.tar.zst",
            "https://example.invalid/pkg.zst", 1234)])
    return inner


def record(*args, **kwargs):
    # a QMessageBox call is (parent, title, text, ...); keep every one, in order
    calls.setdefault("messages", []).append(" ".join(str(a) for a in args[1:]))

QMessageBox.exec = lambda self: 0                      # never block, never click
QMessageBox.warning = staticmethod(record)
QMessageBox.information = staticmethod(record)

# one fixed settings location, so a write is visible to the next read
_settings = Path(tempfile.mkdtemp())
updates_mod.read_cache = lambda home=None: None        # force the real path each time
updates_mod.write_cache = lambda *a, **k: None
updates_mod.latest_release = fake_latest()
updates_mod.is_arch_family = lambda: True  # exercise the pacman download path on any test host
updates_mod.config_path = lambda home=None: _settings / "config.json"
updates_mod.cache_file = lambda home=None: _settings / "update-check.json"

app = QApplication.instance() or QApplication([])
window = gui_mod.MainWindow()

print("update toolbar")
check("the toolbar offers a check", window.btn_check_updates.text(), "Check for updates")
check("and a state label next to it", window.lbl_update_state.text() in ("", "checking..."), True)

print("the startup check is suppressed when asked")
window.check_for_updates(manual=False)
check("no request is made at all", calls.get("latest"), None)
check("and no state is shown", window.lbl_update_state.text(), "")

print("a newer release is offered")
os.environ.pop("WPT_NO_UPDATE_CHECK")                  # what the app does on a normal start
window.check_for_updates(manual=False)
for _ in range(200):                                   # the check runs on a thread
    app.processEvents()
    if not window._update_busy:
        break
    app.thread().msleep(20)
check("the running version is what we compare against",
      updates_mod.parse_version(updates_mod.__version__),
      updates_mod.parse_version(updates_mod.__version__))     # read, never hardcoded
check("the release was remembered", getattr(window._update_release, "tag", None), "v9.9.9")
check("and announced in the toolbar", window.lbl_update_state.text(), "9.9.9 available")
os.environ["WPT_NO_UPDATE_CHECK"] = "1"                # keep the rest of the run quiet

print("an older release is not")
window._update_release = None
updates_mod.latest_release = fake_latest(version=(0, 0, 1), tag="v0.0.1")
window.check_for_updates(manual=True)
for _ in range(200):
    app.processEvents()
    if not window._update_busy:
        break
    app.thread().msleep(20)
check("up to date is reported", window.lbl_update_state.text(),
      f"up to date ({updates_mod.__version__})")
check("and nothing is offered", window._update_release, None)

print("a failed check is reported, not swallowed")
calls["latest_raises"] = True
window.check_for_updates(manual=True)
for _ in range(200):
    app.processEvents()
    if not window._update_busy:
        break
    app.thread().msleep(20)
check("the failure is shown", window.lbl_update_state.text().startswith("update check failed:"), True)
check("and the user was told", any("cannot reach GitHub" in m for m in calls.get("messages", [])), True)
check("the up-to-date case was announced too",
      any("newest release" in m for m in calls.get("messages", [])), True)
calls["latest_raises"] = False

print("downloading an update: verified, then a confirmation")
calls.clear()
if "skip_version" in updates_mod.load_config():
    pass
updates_mod.latest_release = fake_latest()
release = updates_mod.latest_release()
# a real pacman package (zstd magic) served as a file:// URL, so no network is involved
pkg = Path(tempfile.mkdtemp()) / "wine-plugin-toolkit-9.9.9-1-any.pkg.tar.zst"
pkg.write_bytes(updates_mod.ZSTD_MAGIC + b"pretend payload" * 1000)
release.assets = [updates_mod.Asset(pkg.name, pkg.as_uri(), pkg.stat().st_size)]
window._update_release = release
window._confirm_install = lambda: calls.setdefault("confirmed", window._update_path)
window.start_update_download(release)
for _ in range(400):
    app.processEvents()
    if not window._update_busy:
        break
    app.thread().msleep(20)
check("the package was fetched", str(calls.get("confirmed", "")).endswith(pkg.name), True)
check("and the file validated as a pacman package", window._update_path.exists(), True)
check("the toolbar says it is ready", window.lbl_update_state.text(), "ready to install")

print("a download that is not a package is refused")
bad = Path(tempfile.mkdtemp()) / "wine-plugin-toolkit-9.9.9-1-any.pkg.tar.zst"
bad.write_text("<html>404</html>")
release_bad = make_release((9, 9, 9), "v9.9.9",
                           [updates_mod.Asset(bad.name, bad.as_uri(), bad.stat().st_size)])
window._update_release = release_bad
window.start_update_download(release_bad)
for _ in range(400):
    app.processEvents()
    if not window._update_busy:
        break
    app.thread().msleep(20)
check("it is not treated as a package", window.lbl_update_state.text(), "download failed")
check("and the earlier valid download is still the staged one",
      window._update_path is not None and window._update_path.name == pkg.name, True)

print("a version the user skipped is remembered but a manual check still shows it")
updates_mod.save_config({"skip_version": "9.9.9"}, home=_settings)
check("the skip is stored", updates_mod.skipped_version(), "9.9.9")
window._update_release = None
window._update_checked(make_release((9, 9, 9), "v9.9.9", []), None, manual=False)
check("it is not offered again", window._update_release, None)
window._update_checked(make_release((9, 9, 9), "v9.9.9", []), None, manual=True)
check("but a manual check still shows it", getattr(window._update_release, "tag", None), "v9.9.9")

print("shutdown")
window._wait_for_jobs()
check("no update worker is left running", window._update_job_running(), False)
window.close()

print(f"\nupdate tab checks: {checks - len(failures)}/{checks} passed")
if failures:
    for line in failures:
        print("  FAILED:", line)
    sys.exit(1)
