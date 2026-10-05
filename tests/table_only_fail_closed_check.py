#!/usr/bin/env python3
"""Table-only removal must preserve files when only size, not payload bytes, is known."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, leftovers, remove_files  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def table_only_same_size_file_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-table-only-fail-closed-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        bundle = env.vst3_dir / "Plugin.vst3"
        installed = bundle / "Contents" / "plugin.dll"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"other")
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(base / "nominal", bundle, "VST3DIR")],
                    owned_files={installed: MsiFileEntry(
                        "F", "VST3DIR", Path("Plugin.vst3/Contents/plugin.dll"), 5)},
                    destinations_only=True)

        rows = remove_files(plan, env)

        assert any(status == "refused" for status, _path, _note in rows), rows
        assert installed.read_bytes() == b"other", rows
        assert leftovers(plan, env) == [installed]

    print("ok table-only plan refuses size-only deletion and preserves the file")


if __name__ == "__main__":
    table_only_same_size_file_is_refused()
    print("table-only fail-closed check: 1/1 passed")
