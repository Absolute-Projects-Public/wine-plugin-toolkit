#!/usr/bin/env python3
"""Verify a detached WPT child survives its launching Python interpreter exiting."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(label: str, actual, expected) -> None:
    print(f"  {'ok' if actual == expected else 'FAIL'} {label}: {actual!r}")
    if actual != expected:
        failures.append(label)


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_group(pgid: int) -> bool:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and group_exists(pgid):
        time.sleep(0.05)
    if group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and group_exists(pgid):
        time.sleep(0.05)
    return not group_exists(pgid)


def main() -> int:
    global failures
    failures = []
    print("WPT standalone process exit detachment checks")
    with tempfile.TemporaryDirectory(prefix="wpt-detach-parent-exit-") as temp:
        root = Path(temp)
        home = root / "home"
        tree = root / "tree"
        prefix = root / "prefix"
        fake_wine = tree / "bin" / "wine"
        fake_wine.parent.mkdir(parents=True)
        fake_wine.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
        fake_wine.chmod(0o755)
        app = prefix / "drive_c" / "Program Files" / "Synthetic" / "App.exe"
        app.parent.mkdir(parents=True)
        app.write_bytes(b"synthetic executable placeholder")
        helper = root / "launch_then_exit.py"
        helper.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from wpt.environment import Environment\n"
            "from wpt.standalone import launch_standalone\n"
            "root = Path(sys.argv[1])\n"
            "env = Environment(home=root/'home', wine_tree=root/'tree', "
            "prefix=root/'prefix', user='synthetic')\n"
            "app = root/'prefix'/'drive_c'/'Program Files'/'Synthetic'/'App.exe'\n"
            "launch = launch_standalone(env, app)\n"
            "Path(sys.argv[2]).write_text(str(launch.pid))\n"
        )
        pid_file = root / "child.pid"
        child_env = os.environ.copy()
        child_env.update({"HOME": str(home), "XDG_CACHE_HOME": str(root / "cache")})
        pgid = None
        try:
            parent = subprocess.run(
                [sys.executable, str(helper), str(root), str(pid_file)],
                env=child_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            check("launching interpreter exited successfully", parent.returncode, 0)
            if parent.stderr:
                print(parent.stderr, file=sys.stderr, end="")
            if not pid_file.is_file():
                check("launcher recorded the detached child PID", False, True)
            else:
                pgid = int(pid_file.read_text().strip())
                try:
                    check("child is its own process-group leader", os.getpgid(pgid), pgid)
                    check("child is in its own session", os.getsid(pgid), pgid)
                    check("child remains alive after launcher interpreter exits", group_exists(pgid), True)
                except ProcessLookupError:
                    check("child remains alive after launcher interpreter exits", False, True)
            logs = list((root / "cache" / "wpt" / "standalone").glob("*.log"))
            check("launch output stays in the scratch XDG cache", bool(logs), True)
        finally:
            # The only signal target is the process group just created by the fake launcher.
            if pid_file.is_file():
                pgid = int(pid_file.read_text().strip())
            if pgid is not None:
                check("scratch process group is cleaned up", stop_group(pgid), True)

    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("standalone process exit detachment checks: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
