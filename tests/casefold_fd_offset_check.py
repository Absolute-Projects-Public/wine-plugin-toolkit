#!/usr/bin/env python3
"""Case-fold lookup must see entries created after an earlier scan on the same fd."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.installer import _casefold_existing_name  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-fd-") as tmp:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(tmp, flags)
        try:
            assert _casefold_existing_name(parent_fd, "Program Files") is None
            os.mkdir("Program Files", 0o755, dir_fd=parent_fd)
            assert _casefold_existing_name(parent_fd, "Program Files") == "Program Files"
        finally:
            os.close(parent_fd)

    print("casefold fd offset check: 1/1 passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
