"""Regressions for the GUI stability review (2026-09-27).

Defects a review reproduced, each pinned here so it cannot come back:

1. the uninstall pre-check worker had no `on_failed`, so an unreadable MSI left its button
   disabled with nothing in the log to explain it;
2. `refresh_downloads(background=True)` fell through to the synchronous msitools scan on the
   GUI thread whenever a scan was already in flight (`_watch_tick` hits exactly that);
3. `blank_machine_state()` emptied the plugin table without putting the empty-state note back,
   leaving an empty grid on screen for the README renders;
4. `run_install()` did the same when it cleared the install table;
5. `find_msis()` ran one `msiinfo` read per cached MSI on the GUI thread;
6. `toggle_selected()` renamed files inside the prefix while an install/uninstall job was
   running, instead of being declined like every other action;
7. `btn_source_note` carried an inline `color: palette(mid)` stylesheet (1.25:1 on Breeze Dark)
   and stayed blank until the user changed the preset-source combo.

    QT_QPA_PLATFORM=offscreen WPT_NO_UPDATE_CHECK=1 python3 tests/gui_job_failure_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["WPT_NO_UPDATE_CHECK"] = "1"

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from wpt import gui as gui_mod  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'}  {name}")


QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.exec = lambda self: 0

app = QApplication.instance() or QApplication([])
main_thread = threading.get_ident()

# a prefix-shaped stand-in: `blank_machine_state` and the downloads scan walk these paths
sandbox = Path(tempfile.mkdtemp(prefix="wpt-job-check-"))
prefix = sandbox / ".wine-ableton"
for sub in ("drive_c/Program Files", "drive_c/ProgramData", "drive_c/users/tester/AppData/Roaming",
            "drive_c/Program Files/Common Files/VST3", "drive_c/Program Files/VstPlugins"):
    (prefix / sub).mkdir(parents=True, exist_ok=True)
ENV = types.SimpleNamespace(
    prefix=prefix, drive_c=prefix / "drive_c",
    program_files=prefix / "drive_c/Program Files",
    program_data=prefix / "drive_c/ProgramData",
    appdata_roaming=prefix / "drive_c/users/tester/AppData/Roaming",
    vst3_dir=prefix / "drive_c/Program Files/Common Files/VST3",
    vst2_dir=prefix / "drive_c/Program Files/VstPlugins",
    aax_dir=prefix / "drive_c/Program Files/Common Files/Avid/Audio/Plug-Ins",
)

window = gui_mod.MainWindow()
app.processEvents()
window.env = ENV


def drain(timeout: float = 20.0) -> None:
    end = time.time() + timeout
    while window._jobs_running() and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


class FakeEntry:
    """The little of an inventory entry the plugin tab reads."""

    def __init__(self, msi: Path) -> None:
        self.msi = msi
        self.name = "Archetype Test X.vst3"
        self.disabled = False
        self.integrity = "ok"


msi_path = sandbox / "Archetype Test X.msi"
msi_path.write_bytes(b"not a real msi")
window._selected_entry = lambda: FakeEntry(msi_path)


def raise_it(*_a, **_k):
    raise RuntimeError("msitools could not read this MSI")


print("1. a failing uninstall pre-check hands its button back and says why")
_real_identity = gui_mod.msi_mod.identity
gui_mod.msi_mod.identity = raise_it
window.btn_uninstall.setEnabled(True)
before = window.plugin_log.toPlainText()
window.uninstall_selected()
check("the pre-check worker started", window._jobs_running(), True)
drain()
check("the button is usable again", window.btn_uninstall.isEnabled(), True)
check("the failure reached the log",
      "msitools could not read this MSI" in window.plugin_log.toPlainText()[len(before):], True)
gui_mod.msi_mod.identity = _real_identity

print("2. a second background downloads scan never runs on the GUI thread")
threads: list[int] = []


def slow_scan():
    threads.append(threading.get_ident())
    time.sleep(0.5)
    return set(), {}


_real_scan = window._scan_prefix_and_downloads
window._scan_prefix_and_downloads = slow_scan
window._downloads_scan_running = False
window.refresh_downloads(background=True)
for _ in range(100):
    app.processEvents()
    if threads:
        break
    time.sleep(0.02)
started = time.time()
window.refresh_downloads(background=True)      # arrives mid-scan, as _watch_tick does
elapsed = time.time() - started
check("the second request returned immediately", elapsed < 0.3, True)
check("it started no second scan", len(threads), 1)
check("and ran nothing on the GUI thread", all(t != main_thread for t in threads), True)
drain()
window._scan_prefix_and_downloads = _real_scan

print("3. emptying the plugin table puts its note back")
window.plugin_table.insertRow(0)
window.plugin_table.setItem(0, 0, gui_mod.QTableWidgetItem("ok"))
gui_mod._show_rows(window.plugin_table, window.plugin_empty, window.plugin_table.rowCount())
check("table shown while it has rows", window.plugin_table.isHidden(), False)
window.blank_machine_state()
app.processEvents()
check("blanked table hidden again", window.plugin_table.isHidden(), True)
check("and the note stands in its place", window.plugin_empty.isHidden(), False)

print("4. 'Find in prefix' reads the MSIs off the GUI thread")
seen: list[int] = []


def slow_find(*_a, **_k):
    seen.append(threading.get_ident())
    time.sleep(0.5)
    return []


_real_find = gui_mod.msi_mod.find_extracted_msis
gui_mod.msi_mod.find_extracted_msis = slow_find
started = time.time()
window.find_msis()
elapsed = time.time() - started
check("the click returned without blocking", elapsed < 0.3, True)
drain()
check("the MSI reads ran off the GUI thread",
      bool(seen) and all(t != main_thread for t in seen), True)
gui_mod.msi_mod.find_extracted_msis = _real_find

print("5. clearing the install table puts its note back too")
window.install_table.insertRow(0)
window.install_table.setItem(0, 0, gui_mod.QTableWidgetItem("x"))
gui_mod._show_rows(window.install_table, window.install_empty, 1)
check("table shown after a preview", window.install_table.isHidden(), False)
window.msi_combo.addItem(str(msi_path))
_real_build_plan = gui_mod.build_plan
gui_mod.build_plan = lambda *a, **k: gui_mod.installer_mod.Plan(
    msi=msi_path, identity=None, expected={})
gui_mod.msi_mod.extract = lambda *a, **k: sandbox
window.run_install(dry_run=True)
app.processEvents()
check("cleared install table hidden", window.install_table.isHidden(), True)
check("install note shown in its place", window.install_empty.isHidden(), False)
drain()
gui_mod.build_plan = _real_build_plan

print("6. Disable is declined while a job is running against the prefix")
renamed: list[str] = []
_real_set_enabled = gui_mod.set_plugin_enabled
gui_mod.set_plugin_enabled = lambda env, name, enabled=True: renamed.append(name) or []
started_flag: list[bool] = []


def slow_job(emit):
    started_flag.append(True)
    time.sleep(1.2)
    return "done"


window._spawn(slow_job, log=window.plugin_log, label="install")
for _ in range(100):
    app.processEvents()
    if started_flag:
        break
    time.sleep(0.02)
check("a job is running", window._jobs_running(), True)
before = window.plugin_log.toPlainText()
window.toggle_selected(enabled=False)
check("the rename did not run", renamed, [])
check("and the decline was explained",
      "still working on a job against the prefix" in window.plugin_log.toPlainText()[len(before):],
      True)
drain()
window.toggle_selected(enabled=False)
check("once the job finished the rename is allowed again", len(renamed), 1)
gui_mod.set_plugin_enabled = _real_set_enabled

print("7. the preset-source note is populated at startup and carries no stylesheet")
fresh = gui_mod.MainWindow()
app.processEvents()
check("the note is not blank on a cold start", bool(fresh.btn_source_note.text()), True)
check("and it has no inline palette() stylesheet",
      "palette(mid)" in fresh.btn_source_note.styleSheet(), False)
check("the combo opens on a real source", bool(fresh.source_combo.currentData()), True)
fresh.close()

print("8. leaving while a job runs offers a visible way out, and takes it away again")
closing = gui_mod.MainWindow()
closing.env = ENV
closing.show()
app.processEvents()
started_again: list[bool] = []


def another_slow_job(emit):
    started_again.append(True)
    time.sleep(1.0)
    return "done"


closing._spawn(another_slow_job, log=closing.plugin_log, label="install")
for _ in range(100):
    app.processEvents()
    if started_again:
        break
    time.sleep(0.02)
check("the close was refused while the job ran", closing.close(), False)
app.processEvents()
check("a dialog explains the wait", closing._closing_dialog is not None, True)
check("and it is on screen", closing._closing_dialog.isVisible(), True)
buttons = [b.text() for b in closing._closing_dialog.findChildren(gui_mod.QPushButton)]
check("with the explicit way out", any("Force quit" in text for text in buttons), True)
drain_other = time.time() + 20
while closing._jobs_running() and time.time() < drain_other:
    app.processEvents()
    time.sleep(0.02)
for _ in range(80):
    app.processEvents()
    if closing._closing_dialog is None:
        break
    time.sleep(0.02)
check("the dialog takes itself away once the prefix is free", closing._closing_dialog, None)
check("and the window closed itself", closing.isVisible(), False)

print("9. force quit does nothing unless it is confirmed")
guarded = gui_mod.MainWindow()
guarded.env = ENV
started_third: list[bool] = []


def third_job(emit):
    started_third.append(True)
    time.sleep(1.5)
    return "done"


guarded._spawn(third_job, log=guarded.plugin_log, label="install")
for _ in range(100):
    app.processEvents()
    if started_third:
        break
    time.sleep(0.02)
_real_warning = QMessageBox.warning
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
_real_exit = os._exit
exited: list[int] = []
os._exit = lambda code: exited.append(code)
guarded._force_quit()
QMessageBox.warning = _real_warning
check("declining the confirmation exits nothing", exited, [])
check("and leaves the job running", guarded._jobs_running(), True)

QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
guarded._force_quit()
QMessageBox.warning = _real_warning
check("confirming it stops the worker and exits", exited, [130])
check("no QThread is left running", guarded._jobs_running(), False)
os._exit = _real_exit
guarded.close()
guarded._closing_dialog = None

print(f"jobs still running on the way out: {window._jobs_running()}")
window.close()
app.processEvents()

print(f"\nGUI job-failure checks: {checks - len(failures)}/{checks} passed")
if failures:
    for line in failures:
        print("  FAILED:", line)
    sys.exit(1)
