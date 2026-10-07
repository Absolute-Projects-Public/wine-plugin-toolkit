"""test_core must not delete a same-named directory from the invoking user's HOME."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wpt-test-home-safety-") as temp:
        home = Path(temp) / "home"
        protected = home / "wpt-scratch-refusal-test"
        protected.mkdir(parents=True)
        sentinel = protected / "important.txt"
        sentinel.write_text("user data must survive", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("WPT_")}
        env["HOME"] = str(home)
        env["TMPDIR"] = str(Path(temp) / "tmp")
        Path(env["TMPDIR"]).mkdir()
        env["XDG_CACHE_HOME"] = str(home / ".cache")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_STATE_HOME"] = str(home / ".local" / "state")
        env.pop("WINEPREFIX", None)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "test_core.py")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert sentinel.is_file(), "test_core removed a same-named user directory"
        assert sentinel.read_text(encoding="utf-8") == "user data must survive"
        assert result.returncode == 0, result.stdout + result.stderr
    print("test_core HOME isolation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
