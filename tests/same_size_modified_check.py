#!/usr/bin/env python3
"""A same-size local edit must not be uninstalled as if it matched the MSI payload."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, leftovers, remove_files  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def same_size_modified_file_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-same-size-edit-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        payload = base / "payload" / "Plugin.vst3"
        bundle = env.vst3_dir / "Plugin.vst3"
        source = payload / "Contents" / "plugin.dll"
        installed = bundle / "Contents" / "plugin.dll"
        source.parent.mkdir(parents=True)
        installed.parent.mkdir(parents=True)
        source.write_bytes(b"known")
        installed.write_bytes(b"other")  # same size, different bytes
        entry = MsiFileEntry("F", "VST3DIR", Path("Plugin.vst3/Contents/plugin.dll"), 5)
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(payload, bundle, "VST3DIR")], owned_files={installed: entry})

        rows = remove_files(plan, env)

        assert any(status == "refused" for status, _path, _note in rows), rows
        assert installed.read_bytes() == b"other", rows
        assert leftovers(plan, env) == [installed]

    print("ok same-size modified file is refused and preserved")


if __name__ == "__main__":
    same_size_modified_file_is_refused()
    print("same-size modified check: 1/1 passed")
