"""Doctor must honor global environment overrides while keeping its checks read-only."""
from __future__ import annotations

import io
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wpt import cli, doctor


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wpt-doctor-override-") as temporary:
        root = Path(temporary)
        home = root / "home"
        prefix = root / "prefix"
        tree = root / "wine-d2d1-nspa-test"
        scratch = root / "scratch"
        for path in (
            home,
            prefix / "drive_c",
            tree / "bin",
            scratch,
        ):
            path.mkdir(parents=True)
        (prefix / "system.reg").write_text("", encoding="utf-8")
        (prefix / "user.reg").write_text("", encoding="utf-8")
        (tree / "bin" / "wine").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (tree / "bin" / "wine").chmod(0o755)

        argv = [
            "wpt", "--home", str(home), "--prefix", str(prefix),
            "--tree", str(tree), "--user", "test-user", "doctor",
            "--scratch", str(scratch),
        ]
        previous_argv = sys.argv
        output = io.StringIO()
        try:
            sys.argv = argv
            with ExitStack() as stack:
                for name in (
                    "check_tools",
                    "check_directories",
                    "check_msi_sources",
                    "check_inventory",
                    "check_wrappers",
                    "check_scratch",
                    "check_gui",
                    "check_rescued_presets",
                ):
                    stack.enter_context(patch.object(doctor, name, lambda *args, **kwargs: None))
                with redirect_stdout(output):
                    result = cli.main()
        finally:
            sys.argv = previous_argv

        rendered = output.getvalue()
        expected = f"tree {tree.name}, prefix {prefix}, wine user 'test-user'"
        assert expected in rendered, rendered
        assert f"under {home / '.local/opt'}" not in rendered, rendered
        assert result == 0, rendered

        failed = doctor.Report()
        assert doctor.check_environment(failed, home=home, prefix=prefix, user="test-user") is None
        assert failed.checks[0].fix == (
            "Is this an ableton-linux Wine prefix? Use global overrides before the subcommand: "
            "wpt --prefix PREFIX --tree TREE doctor."
        )

    print("doctor override check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
