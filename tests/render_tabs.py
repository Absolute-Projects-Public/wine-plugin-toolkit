"""Render the two tabs to PNGs offscreen so the hint text can actually be looked at.

His report: *"the tooltip info for the download page (instructions) is hidden, either placement error
or possible text colour?"* — a screenshot answers that in one look.

    QT_QPA_PLATFORM=offscreen python3 tests/render_tabs.py /tmp
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402


def main(where: str = "/tmp") -> int:
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

    tabs = window.centralWidget()
    for index, name in ((1, "plugins"), (3, "downloads")):
        tabs.setCurrentIndex(index)
        app.processEvents()
        path = out / f"tab-{name}.png"
        window.grab().save(str(path))
        print(f"wrote {path}")
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
