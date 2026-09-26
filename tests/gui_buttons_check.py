"""Click every button in every tab. Bugs of the "Qt passes an argument you did not expect"
kind (a `clicked` signal handing a `checked` bool to a slot that took a release) show up here
and nowhere else — his traceback, 2026-09-26:

    AttributeError: 'bool' object has no attribute 'windows'

Each click must either do something it can explain in its log, or be disabled. Anything that
raises is a failure.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_buttons_check.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402

failures: list[str] = []


def main() -> int:
    app = QApplication([])
    window = MainWindow()
    app.processEvents()
    window.refresh_env()
    window.refresh_plugins()
    window.refresh_downloads()
    window.refresh_pending()
    for _ in range(30):
        app.processEvents()

    checked = 0
    skipped_destructive: list[str] = []
    for tab in range(window.centralWidget().count()):
        name = window.centralWidget().tabText(tab)
        window.centralWidget().setCurrentIndex(tab)
        app.processEvents()
        for button in window.centralWidget().widget(tab).findChildren(QPushButton):
            label = button.text()
            if not button.isEnabled():
                print(f"  skip  [{name}] {label!r} (disabled)")
                continue
            # Not for an automated click: anything that changes the prefix, and anything that
            # opens a modal dialog (a file picker would sit there until the timeout).
            skip_words = ("uninstall", "install ", "install\u2026", "repair", "browse", "…")
            if any(word in label.lower() for word in skip_words):
                skipped_destructive.append(f"[{name}] {label}")
                continue
            try:
                button.click()
                app.processEvents()
                checked += 1
                print(f"  ok    [{name}] {label!r} clicked without raising")
            except Exception:  # noqa: BLE001 - the point is to catch it
                failures.append(f"[{name}] {label}")
                print(f"  FAIL  [{name}] {label!r}")
                traceback.print_exc()

        for box in window.centralWidget().widget(tab).findChildren(QCheckBox):
            try:
                box.setChecked(not box.isChecked())
                app.processEvents()
                box.setChecked(not box.isChecked())
                app.processEvents()
            except Exception:  # noqa: BLE001
                failures.append(f"[{name}] checkbox {box.text()!r}")
                print(f"  FAIL  [{name}] checkbox {box.text()!r}")
                traceback.print_exc()

    # let any job those clicks started finish, the way a user would before closing the window
    import time

    deadline = time.time() + 120
    while window._jobs_running() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)

    print()
    print(f"clicked {checked} button(s); left alone for safety: {', '.join(skipped_destructive) or 'none'}")
    window.close()
    app.processEvents()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("button check: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
