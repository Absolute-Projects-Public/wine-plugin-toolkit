#!/usr/bin/env python3
"""Copy execution must not follow a destination symlink outside the Wine prefix."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, apply_plan  # noqa: E402
from wpt.msi import MsiIdentity  # noqa: E402


def nested_destination_symlink_cannot_escape_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-copy-symlink-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "Plugin.vst3"
        payload_file = source / "Contents" / "Resources" / "plugin.dat"
        payload_file.parent.mkdir(parents=True)
        payload_file.write_bytes(b"must stay in prefix")

        dest = env.vst3_dir / "Plugin.vst3"
        dest.mkdir(parents=True)
        outside = base / "outside"
        outside.mkdir()
        (dest / "Contents").symlink_to(outside, target_is_directory=True)

        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(source=source, dest=dest, label="VST3DIR", no_clobber=True)])
        rows = apply_plan(plan, env, dry_run=False)

        assert not (outside / "Resources" / "plugin.dat").exists(), rows
        assert rows[0][0] != "copied", rows

    print("ok nested destination symlink cannot redirect a copy outside the prefix")


def no_clobber_copy_still_writes_regular_files() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-copy-regular-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "Plugin.vst3"
        payload_file = source / "Contents" / "plugin.dat"
        payload_file.parent.mkdir(parents=True)
        payload_file.write_bytes(b"safe payload")
        dest = env.vst3_dir / "Plugin.vst3"
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(source=source, dest=dest, label="VST3DIR", no_clobber=True)])

        rows = apply_plan(plan, env, dry_run=False)

        assert rows[0][0] == "copied", rows
        assert (dest / "Contents" / "plugin.dat").read_bytes() == b"safe payload"

    print("ok no-clobber copies regular payload files")


def overwriting_copy_cannot_follow_nested_destination_symlink() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-copy-overwrite-symlink-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "Plugin.vst3"
        payload_file = source / "Contents" / "Resources" / "plugin.dat"
        payload_file.parent.mkdir(parents=True)
        payload_file.write_bytes(b"must stay in prefix")

        dest = env.vst3_dir / "Plugin.vst3"
        dest.mkdir(parents=True)
        outside = base / "outside"
        outside.mkdir()
        (dest / "Contents").symlink_to(outside, target_is_directory=True)
        sentinel = outside / "Resources" / "plugin.dat"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"outside data")

        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(source=source, dest=dest, label="VST3DIR", no_clobber=False)])
        rows = apply_plan(plan, env, dry_run=False)

        assert sentinel.read_bytes() == b"outside data", (rows, sentinel.read_bytes())
        assert rows[0][0] != "copied", rows
    print("ok overwriting copy refuses nested destination symlinks without writing outside")


def overwriting_copy_replaces_existing_regular_file() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-copy-overwrite-regular-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "Plugin.vst3"
        payload_file = source / "Contents" / "plugin.dat"
        payload_file.parent.mkdir(parents=True)
        payload_file.write_bytes(b"new plugin data")
        dest = env.vst3_dir / "Plugin.vst3"
        existing = dest / "Contents" / "plugin.dat"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old plugin data")
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(source=source, dest=dest, label="VST3DIR")])

        rows = apply_plan(plan, env, dry_run=False)

        assert rows[0][0] == "copied", rows
        assert existing.read_bytes() == b"new plugin data"
    print("ok no-follow overwrite still replaces an existing regular payload file")


if __name__ == "__main__":
    nested_destination_symlink_cannot_escape_prefix()
    no_clobber_copy_still_writes_regular_files()
    overwriting_copy_cannot_follow_nested_destination_symlink()
    overwriting_copy_replaces_existing_regular_file()
    print("apply plan symlink check: 4/4 passed")
