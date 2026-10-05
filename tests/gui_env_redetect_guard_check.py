"""Re-detection must not change the target prefix while GUI work is queued or active."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["WPT_NO_UPDATE_CHECK"] = "1"

from PySide6.QtWidgets import QApplication  # noqa: E402
from wpt import gui as gui_mod  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    window = gui_mod.MainWindow()
    if window.env is None:
        window.close()
        print("FAIL: scratch HOME has no detected Wine environment")
        return 1

    original_env = window.env
    window._workers.append(cast(gui_mod.Worker, object()))
    try:
        with patch.object(gui_mod, "detect", side_effect=AssertionError("detect must not run during a job")):
            window.refresh_env()
        if window.env is not original_env:
            print("FAIL: Re-detect changed the environment during queued work")
            return 1
        if "paused" not in window.env_note.text().lower():
            print("FAIL: Re-detect did not explain why it was blocked")
            return 1
        print("ok Re-detect is blocked while a background worker is retained")
    finally:
        window._workers.clear()
        window.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
