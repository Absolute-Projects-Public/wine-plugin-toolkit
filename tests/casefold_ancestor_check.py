#!/usr/bin/env python3
"""An exact file spelling is ambiguous when an ancestor has a case-colliding sibling."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, remove_files, verify_plan  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def ancestor_case_collision_refuses_verification_and_removal() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-ancestor-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        vendor = env.vst3_dir / "Vendor"
        collision = env.vst3_dir / "vendor"
        expected = vendor / "Plugin.vst3" / "Contents" / "plugin.dll"
        peer = collision / "Other.vst3" / "Contents" / "other.dll"
        expected.parent.mkdir(parents=True)
        peer.parent.mkdir(parents=True)
        expected.write_bytes(b"owned")
        peer.write_bytes(b"other")
        bundle = vendor / "Plugin.vst3"
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(base / "source", bundle, "VST3DIR")],
                    owned_files={expected: MsiFileEntry(
                        "F", "VST3DIR", Path("Plugin.vst3/Contents/plugin.dll"), 5)})

        checks = verify_plan(plan, env)
        rows = remove_files(plan, env)

        assert checks[0][0] == "ambiguous", checks
        assert any(state == "refused" for state, _path, _note in rows), rows
        assert expected.read_bytes() == b"owned", rows
        assert peer.read_bytes() == b"other", rows

    print("ok ancestor case collision is ambiguous and neither file is removed")


if __name__ == "__main__":
    ancestor_case_collision_refuses_verification_and_removal()
    print("casefold ancestor check: 1/1 passed")
