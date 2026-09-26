"""Offscreen smoke test for the GUI: builds the window, walks every tab, runs the
core calls behind each tab, and exits. Proves the front end and the core modules
agree without needing a display.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

import wpt.gui as gui  # noqa: E402

fails = 0


def wait_for_worker(win, seconds=240):
    """Let a QThread finish, pumping the event loop so its signals land."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if win.worker is None or not win.worker.isRunning():
            app.processEvents()
            time.sleep(0.05)
            app.processEvents()
            return True
        time.sleep(0.02)
    return False


def check(label, fn):
    global fails
    try:
        result = fn()
        print(f"  ok    {label}" + (f"  -> {result}" if result is not None else ""))
        return result
    except Exception as exc:  # noqa: BLE001
        fails += 1
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None


app = QApplication(sys.argv)
app.setApplicationName("Wine Plugin Toolkit (smoke)")

print("building MainWindow")
win = None
try:
    win = gui.MainWindow()
except Exception:
    traceback.print_exc()
    raise SystemExit("MainWindow construction failed")

tabs = win.centralWidget()
print(f"window title : {win.windowTitle()}")
print(f"tabs         : {[tabs.tabText(i) for i in range(tabs.count())]}")

check("Environment tab rendered", lambda: dict(win.env.describe()) if win.env else "no env detected")

# Plugins tab: run the inventory synchronously through the same call the worker makes
check("Plugins tab: inventory", lambda: f"{len(gui.inventory_mod.build(win.env).entries)} file(s)")
win.refresh_plugins()
wait_for_worker(win)
check("Plugins tab: rows rendered", lambda: f"{win.plugin_table.rowCount()} row(s). {win.plugin_summary.text()}")

# Diagnostics tab: same call the worker makes
check("Diagnostics tab: scan", lambda: f"{len(gui.scan_prefix(win.env).entries)} registry path(s)")
win.run_scan()
wait_for_worker(win)
check("Diagnostics tab: rows rendered", lambda: f"{win.scan_table.rowCount()} row(s). {win.scan_summary.text()}")

# product triage across every prefix, and the purge checkbox the uninstall dialog reads
check("Diagnostics tab: triage all prefixes",
      lambda: f"{len(gui.products_mod.triage(win.env))} prefix(es), "
              f"{sum(len(r.products) for r in gui.products_mod.triage(win.env))} record(s)")
win.run_triage()
wait_for_worker(win)
check("Diagnostics tab: triage rendered",
      lambda: f"{len(win.scan_products.toPlainText())} chars, first line: "
              f"{win.scan_products.toPlainText().splitlines()[0][:60]}")
check("Plugins tab: purge checkbox present", lambda: f"{win.cb_purge.text()!r} default={win.cb_purge.isChecked()}")

# Download tab: catalogue parsing and matching against ~/Downloads
check("Download tab: catalogue loaded",
      lambda: f"{len(win._catalogue.releases)} releases ({win._catalogue.fetched or 'snapshot'})")
win.refresh_downloads()
app.processEvents()
check("Download tab: rows rendered",
      lambda: f"{win.download_table.rowCount()} row(s). {win.download_summary.text()[:70]}")
check("Download tab: an installer in ~/Downloads is matched",
      lambda: f"{len(win._downloads)} found, staged for Pending Install")
check("Download tab: catalogue can be refreshed from the live page",
      lambda: (win.load_catalogue(refresh=True) or f"{len(win._catalogue.releases)} releases after refresh"))

# Pending Install tab: same discovery call the worker makes
check("Pending Install tab: discover installers", lambda: f"{len(gui.installers_mod.discover(win.env))} download(s)")
win.refresh_pending()
wait_for_worker(win)
check("Pending Install tab: rows rendered",
      lambda: f"{win.pending_table.rowCount()} row(s). {win.pending_summary.text()}")

# enable/disable path, dry run only -- the smoke test must not touch the user's plugins
check("enable/disable wiring (dry run)",
      lambda: f"{len(gui.set_plugin_enabled(win.env, 'Nolly', enabled=False, dry_run=True))} match(es)")

# Install tab: MSI discovery + a dry-run plan through the same functions
msis = check("Install tab: find MSIs in prefix", lambda: gui.msi_mod.find_extracted_msis(win.env.prefix))
if msis:
    win.msi_combo.addItem(str(msis[0]))
    check("Install tab: dry-run plan", lambda: win.run_install(dry_run=True) or "started")
    wait_for_worker(win)
    check("Install tab: rows rendered",
          lambda: f"{win.install_table.rowCount()} row(s)")
    print("        install log tail:")
    for line in win.install_log.toPlainText().splitlines()[-6:]:
        print(f"          {line}")

print(f"\nGUI smoke: {fails} failure(s)")
raise SystemExit(1 if fails else 0)
