#!/usr/bin/env python3
"""Inventory ownership/size must use full destinations, never a basename shared by MSIs."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import inventory, msi  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Plan  # noqa: E402


def two_distinct_bundles_with_the_same_binary_name() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-inventory-manifest-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        a = env.vst3_dir / "A.vst3/Contents/x86_64-win/Plugin.vst3"
        b = env.vst3_dir / "B.vst3/Contents/x86_64-win/Plugin.vst3"
        for path, content in ((a, b"AAAA"), (b, b"BBBBBBBBB")):
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        msis = [base / "a.msi", base / "b.msi"]
        plans = {}
        for path, owned, size in ((msis[0], a, 4), (msis[1], b, 9)):
            plans[path] = Plan(msi=path, identity=msi.MsiIdentity(product_name=path.stem),
                               owned_files={owned: msi.MsiFileEntry(
                                   "F", "VST3DIR", owned.relative_to(env.vst3_dir), size)})
        with (patch.object(msi, "find_extracted_msis", return_value=msis),
              patch.object(inventory, "build_plan_from_tables", create=True,
                           side_effect=lambda name, *_a, **_k: plans[name])):
            inv = inventory.build(env, include_standalone=False)
        assert len(inv.entries) == 2, inv.entries
        assert {(entry.path, entry.msi, entry.integrity) for entry in inv.entries} == {
            (a, msis[0], "ok"), (b, msis[1], "ok")}, inv.entries
    print("ok same-named binaries in different bundles retain their MSI and size")


if __name__ == "__main__":
    two_distinct_bundles_with_the_same_binary_name()
    print("inventory manifest check: 1/1 passed")
