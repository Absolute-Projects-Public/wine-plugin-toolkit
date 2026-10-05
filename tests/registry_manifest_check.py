#!/usr/bin/env python3
"""Registry purge must not touch a pointer to an unowned file inside a planned folder."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import installer, msi  # noqa: E402
from wpt.environment import Environment  # noqa: E402


def purge_only_msi_owned_file_values() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-registry-ownership-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        artists = env.program_data / "Vendor" / "Plugin" / "Artists"
        artists.mkdir(parents=True)
        owned = artists / "Factory.xml"
        unowned = artists / "My Sound.xml"
        unowned.write_text("my own patch")
        (env.prefix / "system.reg").write_text("fixture")
        plan = installer.Plan(msi=base / "Plugin.msi",
                              identity=msi.MsiIdentity(product_name="Plugin"),
                              expected={"Factory.xml": 2},
                              actions=[installer.Action(artists, artists, "PREDIR")],
                              owned_files={owned: msi.MsiFileEntry(
                                  "F", "PREDIR", Path("Artists/Factory.xml"), 2)})
        sections = {"Software\\Vendor\\Plugin": {"values": {
            "Factory": str(owned), "User": str(unowned)}}}
        with (patch.object(installer.products_mod, "reg_sections", return_value=sections),
              patch.object(installer.products_mod, "to_path", side_effect=lambda _base, value: Path(value))):
            edits = installer.stale_registry_edits(env, plan)
        assert [edit.value for edit in edits] == ["Factory"], edits
    print("ok purge targets an MSI-owned file, not its unowned neighbor")


if __name__ == "__main__":
    purge_only_msi_owned_file_values()
    print("registry manifest check: 1/1 passed")
