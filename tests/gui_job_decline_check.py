"""Two regressions that only show up in the GUI job plumbing.

1. **A declined job must not leave its button dead.** Every action disables its button and then
   asks `_spawn` to run the work; `_spawn` deliberately declines when another job is already
   running. The disabling used to stay, so a click that raced a refresh left a permanently grey
   button and a status line claiming work that never started.
2. **The job must not read widgets.** The install job used to call `self.cb_vst2.isChecked()` and
   friends from the worker thread, which is undefined behaviour in Qt — and would install the
   wrong subset of the payload if the flag read was stale. The options are read on the GUI thread
   and passed as plain values.

    QT_QPA_PLATFORM=offscreen WPT_NO_UPDATE_CHECK=1 python3 tests/gui_job_decline_check.py
"""

from __future__ import annotations

import os
import sys
import time
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


QMessageBox.warning = staticmethod(lambda *a, **k: None)      # no modal dialogs in a test run
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.exec = lambda self: 0

app = QApplication.instance() or QApplication([])
window = gui_mod.MainWindow()
app.processEvents()

# a stand-in for a slow prefix job: it holds the "one at a time" slot for a moment
_slow_ran: list[str] = []


def slow_job(emit):
    _slow_ran.append("started")
    time.sleep(1.2)
    return "done"


print("a job that is already running")
window._spawn(slow_job, log=window.plugin_log, label="slow test job")
for _ in range(60):
    app.processEvents()
    if _slow_ran:          # the thread reported for duty; isRunning() alone can win the race
        break
    time.sleep(0.05)
check("the slow job started", bool(_slow_ran), True)
check("and is registered as running", window._jobs_running(), True)

print("a second action while it runs")
window.plugin_summary.setText("Not scanned yet - press 'Refresh inventory'.")
window.refresh_plugins()                     # disables nothing, but rewrites the status line
check("the status line is not left claiming work",
      window.plugin_summary.text(), "Not scanned yet - press 'Refresh inventory'.")
check("and the decline was explained in the log",
      "still working on the previous one" in window.plugin_log.toPlainText(), True)

# the install buttons: run_install disables them, then _spawn declines
window.msi_combo.addItem("/tmp/definitely-not-a-real.msi")
window.btn_install.setEnabled(True)
window.btn_preview.setEnabled(True)
window.run_install(dry_run=True)
app.processEvents()
check("the install buttons are usable again after the decline",
      (window.btn_install.isEnabled(), window.btn_preview.isEnabled()), (True, True))

print("waiting for the slow job to finish")
deadline = time.time() + 20
while window._jobs_running() and time.time() < deadline:
    app.processEvents()
    time.sleep(0.05)
check("the slow job is done", window._jobs_running(), False)

print("the install options are read on the GUI thread and passed as values")
seen: dict = {}
_real_build_plan = gui_mod.build_plan
_real_apply_plan = gui_mod.apply_plan


def recording_build_plan(msi_path, env, scratch, **options):
    # record what the job was handed and return an empty plan: this test is about the plumbing,
    # not about reading a real installer
    seen["options"] = options
    seen["msi_path"] = msi_path
    return gui_mod.installer_mod.Plan(msi=Path(msi_path), identity=None, expected={}), []


gui_mod.build_plan = recording_build_plan
gui_mod.apply_plan = lambda plan, dry_run=False, **kw: []
_real_spawn = window._spawn


def run_inline(fn, *args, **kwargs):
    """Run the job right here, so the test can inspect what it was handed."""
    fn(lambda _line: None, *args)
    return True


window._spawn = run_inline
window.cb_vst2.setChecked(True)
window.cb_aax.setChecked(False)
gui_mod.msi_mod.extract = lambda *a, **k: Path("/tmp")     # no msiextract in a test run
window.run_install(dry_run=True)
app.processEvents()
check("the job received the four options as plain values",
      sorted(seen.get("options", {}).keys()),
      ["include_aax", "include_presets", "include_standalone", "include_vst2"])
check("and they carry what the window showed",
      (seen["options"]["include_vst2"], seen["options"]["include_aax"]), (True, False))

gui_mod.build_plan = _real_build_plan
gui_mod.apply_plan = _real_apply_plan
window._spawn = _real_spawn
window.close()
app.processEvents()

print(f"\njob plumbing checks: {checks - len(failures)}/{checks} passed")
if failures:
    for line in failures:
        print("  FAILED:", line)
    sys.exit(1)
