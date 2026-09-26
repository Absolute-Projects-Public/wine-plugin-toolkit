"""Checks for the Download tab: the bugs reported (2026-09-26) and its staging-tab layout (2026-09-27).

Three behaviours, each named after what was observed:

1. *"is there a auto refresh on the list about 5 seconds?"* — yes there was, every four seconds,
   and it rebuilt every row. The tick must now do nothing unless the files in ~/Downloads change.
2. *"when clicking on an entry the highlight disappears at times"* — the rebuild cleared the
   selection. A refresh must put it back.
3. *"choosing open download page it either does nothing or opens it (result changes randomly?)"* —
   the button acted on a selection that the rebuild had just cleared, and returned silently when
   there was none.

    QT_QPA_PLATFORM=offscreen python3 tests/gui_downloads_check.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402

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
    app.processEvents()
    window.refresh_downloads()
    for _ in range(60):
        app.processEvents()
        time.sleep(0.1)

    table = window.download_table
    check("the catalogue listed something to click", table.rowCount() > 0, True)

    # --- 2. selection survives a rebuild
    target = 4 if table.rowCount() > 4 else 0
    table.selectRow(target)
    app.processEvents()
    first = window._selected_product_key()
    check("a row can be selected", bool(first), True)

    window.refresh_downloads()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.05)
    check("and it is still selected after a refresh", window._selected_product_key(), first)

    # --- 1. a tick with nothing new must not touch the table at all
    window._seen_downloads = window._download_candidates()
    before_rows = [
        table.item(r, 0).text() for r in range(table.rowCount())
    ]
    selection_before = window._selected_product_key()
    window._watch_tick()
    app.processEvents()
    check("an idle watch tick leaves the rows alone",
          [table.item(r, 0).text() for r in range(table.rowCount())], before_rows)
    check("an idle watch tick leaves the selection alone",
          window._selected_product_key(), selection_before)

    # --- 3. the buttons act on the selection, and say so when there is none
    selected_release, _ = window._selected_release()
    check("the selection resolves to a release", selected_release is not None, True)
    table.clearSelection()
    app.processEvents()
    window.download_log.clear()
    window.download_selected_plugin()
    check("with nothing selected, 'open download page' explains itself",
          "nothing selected" in window.download_log.toPlainText(), True)

    # --- the right-click menu
    menu = window._build_download_menu(selected_release, Path("/home/x/Installer.exe"))
    labels = [action.text() for action in menu.actions()]
    check("the menu offers the download page", "Download Selected Plugin (in browser)" in labels, True)
    check("the menu routes an installer to the staging tab instead of installing from here",
          any(label.startswith("Staged for install:") for label in labels), True)
    check("the menu offers the preset sources",
          any(action.menu() is not None and action.text().startswith("Preset") for action in menu.actions()), True)
    check("the menu offers to copy things",
          all(f"Copy {what}" in labels for what in ("product name", "installer path", "download link")), True)
    sources_menu = next(a.menu() for a in menu.actions() if a.menu() is not None)
    check("every preset source is in that submenu", len(sources_menu.actions()) > 3, True)

    no_installer = window._build_download_menu(selected_release, None)
    check("with nothing downloaded, the menu says so and stays disabled",
          any(a.text().startswith("Not downloaded yet") and not a.isEnabled() for a in no_installer.actions()), True)

    # --- the hint above the list
    hint = window.download_hint.text()
    check("the hint tells the user which button to press", "Download Selected Plugin" in hint, True)
    check("and mentions the right-click menu", "right-click" in hint.lower(), True)
    check("and the browse button", "Browse Plugins In Browser" in hint, True)

    # --- the staging layout he asked for (2026-09-26): this tab fetches, Pending Install installs
    from PySide6.QtWidgets import QPushButton  # noqa: PLC0415

    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    check("the tab order is the workflow",
          names, ["Environment", "Plugins", "Download", "Pending Install", "Install MSI", "Diagnostics"])
    labels_all = [b.text() for b in window.findChildren(QPushButton)]
    check("no install button is left on this tab",
          [x for x in labels_all if "Install downloaded installer" in x], [])
    check("...and no 'watch ~/Downloads' switch either",
          [x for x in labels_all if "watch ~/Downloads" in x], [])
    check("the preset source button is still present and labelled",
          window.btn_source_open.text(), "Open in browser")
    check("the change-detected watch tick runs without a switch on it",
          (window._watch_tick() or "ticked"), "ticked")

    window.close()
    app.processEvents()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("download tab checks: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
