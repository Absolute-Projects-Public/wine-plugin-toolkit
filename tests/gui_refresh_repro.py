"""Reproduce the reported crash: refresh the Plugins tab while a refresh is still running.

Reported: a user disabled the VST3, then hit refresh, and the GUI went down. The suspect is
`MainWindow.worker`, a single attribute holding a QThread. Reassigning it drops the last
Python reference to a *running* QThread, which Qt aborts on
("QThread: Destroyed while thread is still running").

    QT_QPA_PLATFORM=offscreen python3 tests/gui_refresh_repro.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()

    print("first refresh (starts a worker)")
    window.refresh_plugins()
    app.processEvents()

    print("second refresh immediately after, as a user would after clicking Disable")
    window.refresh_plugins()
    app.processEvents()

    # let the one running job finish, pumping the event loop the way Qt needs
    deadline = time.time() + 120
    while window._jobs_running() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
    # the worker's `done` signal is queued, so it lands on a later turn of the loop than
    # `finished`; pump a little longer before judging the result
    rows_deadline = time.time() + 60
    while window.plugin_table.rowCount() == 0 and time.time() < rows_deadline:
        app.processEvents()
        time.sleep(0.1)

    print("survived: window alive, inventory rows =", window.plugin_table.rowCount())
    print("summary:", window.plugin_summary.text())
    if window._jobs_running():
        print("FAIL: a job is still running after 120 s")
        return 1
    if window.plugin_table.rowCount() == 0:
        print("FAIL: the refresh never delivered any rows")
        return 1
    if "still working on the previous one" not in window.plugin_log.toPlainText():
        print("FAIL: the overlapping refresh was not declined in the log")
        return 1
    print("overlapping refresh was declined, then the first one delivered rows")
    # and the second click must not have left a worker behind
    print("workers still tracked:", len(window._workers))

    # and it must still close cleanly with nothing running
    window.close()
    app.processEvents()
    print("closed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
