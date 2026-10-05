#!/usr/bin/env python3
"""msiinfo export's third metadata line is not a File/Directory/Component row."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import msi  # noqa: E402


def third_header_row_is_not_payload() -> None:
    output = ("File\tComponent_\tFileName\tFileSize\n"
              "s72\ts72\tl255\ti4\n"
              "File\tFile\n"
              "real_key\tC1\treal.dll\t17\n")
    with patch.object(msi, "_run", return_value=subprocess.CompletedProcess([], 0, output, "")):
        rows = msi.export_table(Path("fixture.msi"), "File")
    assert rows == [["real_key", "C1", "real.dll", "17"]], rows
    print("ok metadata header is excluded from the File table")


if __name__ == "__main__":
    third_header_row_is_not_payload()
    print("MSI table header check: 1/1 passed")
