#!/usr/bin/env python3
"""Uninstall and leftovers must fail closed when path enumeration raises OSError."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, leftovers, remove_files  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def enumeration_error_becomes_failed_removal_and_remaining_path() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-uninstall-oserror-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        blocked = env.vst3_dir / "Vendor"
        bundle = blocked / "Plugin.vst3"
        owned = bundle / "Contents" / "plugin.dll"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"owned")
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(base / "source", bundle, "VST3DIR")],
                    owned_files={owned: MsiFileEntry(
                        "F", "VST3DIR", Path("Plugin.vst3/Contents/plugin.dll"), 5)})
        real_iterdir = Path.iterdir

        def fail_on_blocked_directory(path: Path):
            if path == blocked:
                raise PermissionError("fixture enumeration denied")
            return real_iterdir(path)

        with patch.object(Path, "iterdir", fail_on_blocked_directory):
            rows = remove_files(plan, env)
            remaining = leftovers(plan, env)

        assert any(status == "failed" for status, _path, _note in rows), rows
        assert remaining, "enumeration failure was misreported as no leftovers"
        assert owned.read_bytes() == b"owned"

    print("ok enumeration errors are reported and the owned file is preserved")


if __name__ == "__main__":
    enumeration_error_becomes_failed_removal_and_remaining_path()
    print("uninstall OSError check: 1/1 passed")
