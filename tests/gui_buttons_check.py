"""Click every button in every tab. Bugs of the "Qt passes an argument you did not expect"
kind (a `clicked` signal handing a `checked` bool to a slot that took a release) show up here
and nowhere else — an observed traceback, 2026-09-26:

    AttributeError: 'bool' object has no attribute 'windows'

Each click must either do something it can explain in its log, or be disabled. Anything that
raises is a failure.

**Nothing this script clicks may touch the prefix.** The buttons that install, uninstall or purge
are clicked too — with the write calls replaced by recording stubs, so the handler runs for real
(that is where the wiring bugs are) while `apply_plan`, `uninstall_product` and `purge_registry`
only record that they were reached. Buttons that open a modal dialog are still skipped: a file
picker would sit there until the timeout.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_buttons_check.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton  # noqa: E402

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from wpt import gui as gui_mod  # noqa: E402
from wpt.gui import MainWindow  # noqa: E402

# No modal dialog may block an offscreen run. `exec()` returning 0 means `clickedButton()` is None,
# which every handler here already treats as "the user chose not to", and the static helpers are
# silenced so a warning does not sit on screen either.
QMessageBox.exec = lambda self: 0
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)

failures: list[str] = []

# what the stubs recorded, so the run can say what it would have done
reached: list[str] = []


def stub(name: str, result):
    def inner(*args, **kwargs):
        reached.append(name)
        return result if not callable(result) else result(*args, **kwargs)
    return inner


def main() -> int:
    app = QApplication([])

    # --- replace every write path with a recorder, BEFORE the window exists
    gui_mod.apply_plan = stub("apply_plan", lambda *a, **k: [])
    gui_mod.uninstall_product = stub("uninstall_product", (0, "stub: nothing uninstalled"))
    gui_mod.purge_registry = stub("purge_registry", [])

    window = MainWindow()
    app.processEvents()
    window.refresh_env()
    window.refresh_downloads()
    for _ in range(30):
        app.processEvents()

    checked = 0
    skipped_modal: list[str] = []
    for tab in range(window.centralWidget().count()):
        name = window.centralWidget().tabText(tab)
        window.centralWidget().setCurrentIndex(tab)
        app.processEvents()
        for button in window.centralWidget().widget(tab).findChildren(QPushButton):
            label = button.text()
            if not button.isEnabled():
                print(f"  skip  [{name}] {label!r} (disabled)")
                continue
            # Modals and pickers only: these cannot be clicked offscreen without hanging. The
            # destructive actions are clicked, but with the write calls stubbed out above.
            modal_words = ("browse", "\u2026", "open in browser", "open download page",
                           "in file manager", "preset & ir sources", "source")
            if any(word in label.lower() for word in modal_words):
                skipped_modal.append(f"[{name}] {label}")
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

    # the toolbar button lives outside the tabs
    try:
        window.btn_check_updates.click()
        app.processEvents()
        checked += 1
        print("  ok    [toolbar] 'Check for updates' clicked without raising")
    except Exception:  # noqa: BLE001
        failures.append("[toolbar] Check for updates")
        traceback.print_exc()

    # let any job those clicks started finish, the way a user would before closing the window
    import time

    deadline = time.time() + 120
    while (window._jobs_running() or window._update_job_running()) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)

    print()
    print(f"clicked {checked} button(s); left alone (they open dialogs): "
          f"{', '.join(skipped_modal) or 'none'}")
    print(f"write calls that would have run: {', '.join(reached) or 'none (no plan was built)'}")
    window.close()
    app.processEvents()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("button check: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
