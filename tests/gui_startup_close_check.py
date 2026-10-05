"""Ensure delayed startup catalogue loading cannot continue after close.

MainWindow schedules its startup callback while constructing the Downloads tab. Close the
window immediately, without processing events, then run Qt's event loop past the 200 ms deadline.
The callback is intercepted before any network or Downloads scan can start.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_startup_close_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402


def main() -> int:
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    if window.env is None:
        print("FAIL: scratch HOME has no detected Wine environment; refusing a modal GUI path")
        window.close()
        return 1

    closed = False
    calls_before_close: list[bool] = []
    calls_after_close: list[bool] = []

    def observe_load(_window: MainWindow, *, refresh: bool = False) -> None:
        # Intercept the callback so the regression cannot trigger network or filesystem work.
        target = calls_after_close if closed else calls_before_close
        target.append(refresh)

    window.load_catalogue = MethodType(observe_load, window)
    window.show()
    close_accepted = window.close()
    closed = True
    if not close_accepted:
        print("FAIL: Qt did not accept the immediate close")
        return 1
    startup_timer = getattr(window, "_startup_catalogue_timer", None)
    if startup_timer is not None and startup_timer.isActive():
        print("FAIL: startup timer remained active after close")
        return 1

    started_at = time.monotonic()
    QTimer.singleShot(700, app.quit)
    app.exec()
    elapsed = time.monotonic() - started_at

    if elapsed < 0.6:
        print(f"FAIL: Qt event loop did not run through the startup deadline ({elapsed:.3f}s)")
        return 1
    if calls_before_close:
        print("FAIL: startup catalogue callback ran before close")
        return 1
    if calls_after_close:
        print(f"FAIL: delayed catalogue callback ran {len(calls_after_close)} time(s) after close")
        return 1
    if window._jobs_running() or window._update_job_running():
        print("FAIL: a worker remains active after close")
        return 1
    print("OK: immediate close; no catalogue callback ran during the 700 ms event loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
