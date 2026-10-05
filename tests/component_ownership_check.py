#!/usr/bin/env python3
"""Optional, conditioned and persistent MSI components lack direct-removal provenance."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import installer, msi  # noqa: E402
from wpt.environment import Environment  # noqa: E402

TABLES = {
    "Directory": [["TARGETDIR", "", "SourceDir"], ["VST3DIR", "TARGETDIR", "VST3DIR"]],
    "Component": [
        ["Optional", "{OPTIONAL}", "VST3DIR", "2", "Installed <> 1", "optional.dll"],
        ["Permanent", "{PERMANENT}", "VST3DIR", "16", "", "permanent.dll"],
        ["Shared", "{SHARED}", "VST3DIR", "2048", "", "shared.dll"],
    ],
    "File": [
        ["FOptional", "Optional", "optional.dll", "7"],
        ["FPermanent", "Permanent", "permanent.dll", "9"],
        ["FShared", "Shared", "shared.dll", "14"],
    ],
}


def file_manifest_retains_component_ownership_metadata() -> None:
    with patch.object(msi, "export_table", side_effect=lambda _msi, table: TABLES[table]):
        entries = msi.file_manifest(Path("fixture.msi"))
    by_key = {entry.file_key: entry for entry in entries}
    assert by_key["FOptional"].component_attributes == 2, by_key["FOptional"]
    assert by_key["FOptional"].component_condition == "Installed <> 1", by_key["FOptional"]
    assert by_key["FPermanent"].component_attributes == 16, by_key["FPermanent"]
    assert by_key["FPermanent"].removal_state_uncertain
    assert by_key["FShared"].component_attributes == 0x0800, by_key["FShared"]
    assert by_key["FShared"].removal_state_uncertain, by_key["FShared"]
    print("ok File manifest retains optional/condition/persistent component metadata")


def install_plan_refuses_optional_component_without_state() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-optional-component-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        root = base / "extract"
        source = root / "VST3DIR" / "Plugin.vst3" / "optional.dll"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"payload")
        entry = msi.MsiFileEntry("FOptional", "VST3DIR", Path("Plugin.vst3/optional.dll"), 7,
                                 component_attributes=2)
        with (patch.object(msi, "identity", return_value=msi.MsiIdentity(
                  product_name="Plugin", manufacturer="Neural DSP")),
              patch.object(msi, "expected_sizes", return_value={}),
              patch.object(msi, "declares_plugin_payload", return_value=True),
              patch.object(msi, "file_manifest", return_value=[entry])):
            try:
                installer.build_plan(base / "fixture.msi", env, root)
            except msi.MsiError as exc:
                assert "component" in str(exc).lower(), str(exc)
            else:
                raise AssertionError("optional component was planned for installation without state")
    print("ok install plan refuses an optional component with unknown installed state")


def direct_removal_preserves_uncertain_component_files() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-component-removal-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "optional.dll"
        target = env.vst3_dir / "Plugin.vst3" / "optional.dll"
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"payload")
        target.write_bytes(b"payload")
        entry = msi.MsiFileEntry("FOptional", "VST3DIR", Path("Plugin.vst3/optional.dll"), 7,
                                 component_attributes=2)
        plan = installer.Plan(
            msi=base / "fixture.msi",
            identity=msi.MsiIdentity(product_name="Plugin"),
            actions=[installer.Action(source.parent, target.parent, "VST3DIR")],
            owned_files={target: entry},
        )
        rows = installer.remove_files(plan, env)
        assert target.read_bytes() == b"payload", rows
        assert any(status == "refused" for status, _path, _note in rows), rows
        assert target in installer.leftovers(plan, env)
    print("ok direct removal preserves a file in an optional component")


def shared_component_files_are_not_directly_removed() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-shared-component-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "shared.dll"
        target = env.vst3_dir / "Plugin.vst3" / "shared.dll"
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source.write_bytes(b"shared payload")
        target.write_bytes(b"shared payload")
        entry = msi.MsiFileEntry("FShared", "VST3DIR", Path("Plugin.vst3/shared.dll"), 14,
                                 component_attributes=0x0800)
        plan = installer.Plan(
            msi=base / "fixture.msi",
            identity=msi.MsiIdentity(product_name="Plugin"),
            actions=[installer.Action(source.parent, target.parent, "VST3DIR")],
            owned_files={target: entry},
        )
        rows = installer.remove_files(plan, env)
        assert entry.removal_state_uncertain, entry
        assert "Shared" in entry.removal_state_reason, entry.removal_state_reason
        assert target.read_bytes() == b"shared payload", rows
        assert any(status == "refused" for status, _path, _note in rows), rows
    print("ok a Windows Installer Shared component is not removed directly")


if __name__ == "__main__":
    file_manifest_retains_component_ownership_metadata()
    install_plan_refuses_optional_component_without_state()
    direct_removal_preserves_uncertain_component_files()
    shared_component_files_are_not_directly_removed()
    print("component ownership check: 4/4 passed")
