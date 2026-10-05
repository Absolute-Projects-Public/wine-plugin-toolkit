"""Layout checks for the GUI pass of 2026-09-26/27: empty tables, column floors, the dark toggle.

Named after what was reported or seen in a render:

1. *A tab with nothing in it showed a large empty grid* (seen at 900x600 on Pending Install, Install
   MSI and Diagnostics) - the grid must be replaced by a sentence, and the table must come back the
   moment there are rows.
2. *Content-sized columns collapse when the table has no rows* - Pending Install drew Status 40 px
   and Kind 35 px, Diagnostics drew Status 40 px, which reads as a broken table.
3. *The five "What to install" checkboxes spread across the whole window* (his screenshot: VST3 hard
   left, AAX hard right) - they belong in a group at the left.
4. *Dark mode should be a header toggle, and must not be compulsory* - off by default (the window
   follows the desktop), reversible without a restart, and remembered.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_layout_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt import updates as updates_mod  # noqa: E402
from wpt.gui import MainWindow, _dark_palette  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


def main() -> int:
    app = QApplication([])
    window = MainWindow()
    window.resize(900, 600)
    window.show()
    app.processEvents()

    # --- 1 & 2: an empty tab explains itself, and its columns keep a floor
    for name, table, note in (
        ("Plugins", window.plugin_table, window.plugin_empty),
        ("Pending Install", window.pending_table, window.pending_empty),
        ("Install MSI", window.install_table, window.install_empty),
        ("Diagnostics", window.scan_table, window.scan_empty),
    ):
        # isVisible() is False for every widget on a tab that is not the current one, so the tab is
        # brought forward first: the question is what the user sees, not what exists
        window.tabs.setCurrentIndex([window.tabs.tabText(i) for i in range(window.tabs.count())].index(name))
        app.processEvents()
        check(f"{name}: an empty table is hidden", table.isVisible(), False)
        check(f"{name}: the note stands in its place", note.isVisible(), True)
        check(f"{name}: its columns keep a minimum width",
              table.horizontalHeader().minimumSectionSize() >= 64, True)

    # --- 1b: rows bring the table back and take the note away
    window.tabs.setCurrentIndex([window.tabs.tabText(i) for i in range(window.tabs.count())].index("Pending Install"))
    window.pending_table.insertRow(0)
    from wpt.gui import _show_rows  # noqa: PLC0415

    _show_rows(window.pending_table, window.pending_empty, window.pending_table.rowCount())
    app.processEvents()
    check("Pending Install: the table returns when it has a row", window.pending_table.isVisible(), True)
    check("Pending Install: and the note steps aside", window.pending_empty.isVisible(), False)
    window.pending_table.setRowCount(0)
    _show_rows(window.pending_table, window.pending_empty, 0)
    app.processEvents()

    # --- 3: the install options are a group at the left, not a spread
    # A hidden tab has not been laid out yet, so select it before measuring geometry.
    window.tabs.setCurrentIndex([window.tabs.tabText(i) for i in range(window.tabs.count())].index("Install MSI"))
    app.processEvents()
    boxes = [window.cb_vst3, window.cb_vst2, window.cb_standalone, window.cb_presets, window.cb_aax]
    gaps = [right.x() - left.geometry().right() - 1 for left, right in zip(boxes, boxes[1:])]
    options_group = boxes[0].parentWidget()
    check("the option checkboxes form a compact group",
          all(0 <= gap <= 12 for gap in gaps), True)
    check("the option group leaves spare space on the right",
          boxes[-1].geometry().right() < options_group.contentsRect().width() - 100, True)

    # --- 4: the dark toggle, its round trip, and its persistence
    written: list[dict] = []
    real_save = updates_mod.save_config
    updates_mod.save_config = lambda values, home=None: written.append(values)
    try:
        dark = _dark_palette()
        check("the dark palette is actually dark",
              dark.color(QPalette.ColorRole.Window).lightness() < 128, True)
        window.apply_theme(True)
        app.processEvents()
        check("toggling dark changes the application palette",
              app.palette().color(QPalette.ColorRole.Window).lightness() < 128, True)
        check("and the header switch follows it", window.cb_dark.isChecked(), True)
        window.apply_theme(False)
        app.processEvents()
        check("turning it off restores the desktop palette",
              app.palette().color(QPalette.ColorRole.Window),
              window._system_palette.color(QPalette.ColorRole.Window))
        check("both choices are written to the config file",
              written, [{"dark_theme": True}, {"dark_theme": False}])
    finally:
        updates_mod.save_config = real_save

    # --- the icon: in the window, in the package, and in the launcher entry
    from wpt.gui import app_icon  # noqa: PLC0415

    icon = app_icon()
    check("the window icon loads from the package", icon.isNull(), False)
    check("and carries the sizes an icon theme asks for",
          sorted(s.width() for s in icon.availableSizes()), [16, 32, 48, 64, 128, 256, 512])
    check("the window is using it", window.windowIcon().isNull(), False)

    desktop = Path(__file__).resolve().parents[1] / "packaging" / "wpt-gui.desktop"
    entry = desktop.read_text() if desktop.exists() else ""
    check("there is a launcher entry", bool(entry), True)
    check("it points at the launcher the package installs", "Exec=wpt-gui" in entry, True)
    check("and at the icon the package installs", "Icon=wpt-gui" in entry, True)
    shipped = Path(__file__).resolve().parents[1] / "wpt" / "data" / "icons"
    check("the package carries the hicolor sizes the PKGBUILD installs",
          sorted(int(f.name.split("-")[1].split(".")[0]) for f in shipped.glob("wpt-*.png")),
          [16, 32, 48, 64, 128, 256, 512])

    # --- the settings surface exists and points at the real config file
    check("there is a settings button", window.btn_about.text(), "Settings & about")
    check("it names the config file the toolkit actually uses",
          updates_mod.config_path().name, "config.json")

    window.close()
    app.processEvents()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("gui layout checks: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
