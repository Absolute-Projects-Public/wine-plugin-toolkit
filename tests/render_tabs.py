"""Render the tabs to PNGs offscreen so the layout can actually be looked at.

Originally written to answer *"the tooltip info for the download page (instructions) is hidden,
either placement error or possible text colour?"*, a screenshot answers that in one look.

    QT_QPA_PLATFORM=offscreen python3 tests/render_tabs.py /tmp

Arguments (all optional, positional in this order):

    <dir>       where the PNGs go (default /tmp)
    <neutral>   anything but "" / 0 / false blanks the machine's own state, for images that go in
                the README, a render of a real prefix shows which plugins this machine has
                installed, which is not something to publish
    <size>      WxH, default 1200x760. Render a small window as well (900x600): a wide window
                hides column problems that a narrow one shows
    <tabs>      "all" (default) or a comma list of indices, e.g. "1,3"

Why all six: `_fit_columns()` styles each table separately, so a tab that was never reworked can
only be judged from its own render.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from wpt.gui import MainWindow  # noqa: E402

def _tabs(window) -> list[tuple[int, str]]:
    """Index -> file stem, read from the window itself.

    A hardcoded list was silently wrong the moment the tabs were reordered (the GUI pass of
    2026-09-26 renamed and moved three of them), and a render named for the wrong tab is worse
    than no render. The tab's own label is the only source that cannot drift.
    """
    stems = {"Download": "download", "Pending Install": "pending-install", "Install MSI": "install-msi"}
    out = []
    for index in range(window.tabs.count()):
        label = window.tabs.tabText(index)
        out.append((index, stems.get(label, label.lower().replace(" ", "-"))))
    return out


def main(where: str = "/tmp", neutral: str = "", size: str = "1200x760", tabs: str = "all") -> int:
    out = Path(where)
    out.mkdir(parents=True, exist_ok=True)
    width, _, height = size.partition("x")
    app = QApplication([])
    window = MainWindow()
    window.resize(int(width), int(height or 760))
    window.show()
    app.processEvents()
    window.refresh_downloads()
    window.refresh_plugins()

    deadline = time.time() + 60
    while (window._jobs_running() or window.download_table.rowCount() == 0) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
    app.processEvents()

    neutral = neutral not in ("", "0", "false")

    def blank() -> None:
        """Show the interface, not this machine's state.

        The window owns this (`MainWindow.blank_machine_state`) so that the README images and
        `WPT_SCREENSHOT_MODE=1` cannot drift apart; it is re-applied before every grab because a
        late worker callback or the 200 ms catalogue timer will otherwise refill the columns from
        the real machine while the renders are still being written.
        """
        window.blank_machine_state()
        app.processEvents()

    if neutral:
        # the periodic watch tick would otherwise re-fill the download table from the real
        # ~/Downloads a few seconds later
        window._watch_timer.stop()

    known = _tabs(window)
    if tabs == "all":
        wanted = known
    else:
        picked = {int(x) for x in tabs.split(",") if x.strip() != ""}
        wanted = [(i, name) for i, name in known if i in picked]

    central = window.centralWidget()
    for index, name in wanted:
        central.setCurrentIndex(index)
        app.processEvents()
        # the tables size themselves when shown, so let the event loop settle before grabbing
        time.sleep(0.25)
        if neutral:
            blank()
        time.sleep(0.05)
        app.processEvents()
        path = out / f"tab-{index}-{name}-{width}x{height or 760}.png"
        window.grab().save(str(path))
        print(f"wrote {path}")

    # a live QThread is aborted by Qt at teardown (SIGABRT, no traceback), so give any
    # background job - the Downloads scan in particular - a chance to finish first
    window._wait_for_jobs()
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
