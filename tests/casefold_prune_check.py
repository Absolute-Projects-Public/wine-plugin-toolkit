#!/usr/bin/env python3
"""Empty case-variant plugin roots must be protected from uninstall pruning."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, remove_files  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def case_variant_vst_root_survives_uninstall_pruning() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-prune-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        actual_root = env.drive_c / "Program Files" / "Common Files" / "vst3"
        bundle = actual_root / "Plugin.vst3"
        owned = bundle / "Contents" / "plugin.dll"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"owned payload")
        relative = Path("Plugin.vst3/Contents/plugin.dll")
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(bundle, bundle, "VST3DIR")],
                    owned_files={owned: MsiFileEntry("F", "VST3DIR", relative,
                                                      len(b"owned payload"))})

        rows = remove_files(plan, env)

        assert not owned.exists(), rows
        assert actual_root.is_dir(), "pruning removed the case-variant shared VST3 root"

    print("ok case-variant shared VST3 root survives uninstall pruning")


if __name__ == "__main__":
    case_variant_vst_root_survives_uninstall_pruning()
    print("casefold prune check: 1/1 passed")
