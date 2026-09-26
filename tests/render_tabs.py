"""Render the two tabs to PNGs offscreen so the hint text can actually be looked at.

Reported: *"the tooltip info for the download page (instructions) is hidden, either placement error
or possible text colour?"* — a screenshot answers that in one look.

    QT_QPA_PLATFORM=offscreen python3 tests/render_tabs.py /tmp
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402


def main(where: str = "/tmp", neutral: str = "") -> int:
    """`neutral=1` blanks what the prefix knows, for images that go in the README.

    A render of a real prefix shows the "In the prefix" column filled in, which says which plugins
    that machine has installed - not something to publish.
    """
    out = Path(where)
    app = QApplication([])
    window = MainWindow()
    window.resize(1200, 760)
    window.show()
    app.processEvents()
    window.refresh_downloads()
    window.refresh_plugins()
    import time

    deadline = time.time() + 60
    while (window._jobs_running() or window.download_table.rowCount() == 0) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
    app.processEvents()

    if neutral not in ("", "0", "false"):
        # clear every source the two columns draw from, so the image shows the interface rather
        # than which plugins this machine has installed and which installers it has downloaded.
        # This runs last: the real refresh above would otherwise fill the columns again.
        window._msi_names = set()
        window._installed_products = set()
        window._downloads = {}
        window._registered_names = lambda: set()
        window._is_installed = lambda release: ""
        window._downloads_for = lambda release: None
        window.download_table.setRowCount(0)
        window._fill_download_table()
        app.processEvents()

    tabs = window.centralWidget()
    for index, name in ((1, "plugins"), (3, "downloads")):
        tabs.setCurrentIndex(index)
        app.processEvents()
        path = out / f"tab-{name}.png"
        window.grab().save(str(path))
        print(f"wrote {path}")
    # a live QThread is aborted by Qt at teardown (SIGABRT, no traceback), so give any
    # background job - the Downloads scan in particular - a chance to finish first
    window._wait_for_jobs()
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
