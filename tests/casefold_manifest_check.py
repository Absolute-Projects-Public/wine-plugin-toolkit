#!/usr/bin/env python3
"""MSI path matching must be case-insensitive yet refuse ambiguous Linux directories."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import (Action, Plan, apply_plan, filter_needing_repair, leftovers, remove_files,
                           verify_plan)  # noqa: E402
from wpt.msi import MsiError, MsiFileEntry, MsiIdentity  # noqa: E402


def plan_at(expected: Path, source: Path, size: int) -> Plan:
    return Plan(msi=Path("fixture.msi"), identity=MsiIdentity(product_name="Plugin"),
                actions=[Action(source, expected, "VST3DIR")],
                owned_files={expected: MsiFileEntry("F", "VST3DIR", Path(expected.name), size)})


def one_case_variant_is_the_same_file() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        expected = env.vst3_dir / "Plugin.ico"
        actual = env.vst3_dir / "plugin.ICO"
        actual.parent.mkdir(parents=True)
        actual.write_bytes(b"DATA")
        source = base / "source"; source.write_bytes(b"DATA")
        plan = plan_at(expected, source, 4)
        assert leftovers(plan, env) == [actual], leftovers(plan, env)
        assert [(state, Path(path)) for state, path, _ in verify_plan(plan, env)] == [("ok", actual)]
        assert filter_needing_repair(plan, env).actions == []
        rows = remove_files(plan, env)
        assert not actual.exists(), rows
        assert leftovers(plan, env) == [], rows
    print("ok case variant is verified and removed as the intended file")


def two_case_variants_are_ambiguous() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        expected = env.vst3_dir / "Plugin.ico"
        alternative = env.vst3_dir / "plugin.ICO"
        expected.parent.mkdir(parents=True)
        for path in (expected, alternative): path.write_bytes(b"DATA")
        source = base / "source"; source.write_bytes(b"DATA")
        plan = plan_at(expected, source, 4)
        assert verify_plan(plan, env)[0][0] == "ambiguous"
        rows = remove_files(plan, env)
        assert any(state == "refused" for state, _path, _note in rows), rows
        assert expected.exists() and alternative.exists(), rows
    print("ok two casefold-equal on-disk names are not guessed at")


def existing_case_variant_bundle_refuses_install() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-install-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        relative = Path("Contents/x86_64-win/Plugin.vst3")
        destination = env.vst3_dir / "Plugin.vst3"
        existing = env.vst3_dir / "plugin.vst3" / relative
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old-version")
        source = base / "payload" / "Plugin.vst3"
        payload = source / relative
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"new-version")
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(source, destination, "VST3DIR")])

        rows = apply_plan(plan, env)

        assert not destination.exists(), rows
        assert existing.read_bytes() == b"old-version", rows
        assert any(status == "failed" for status, _path, _note in rows), rows
    print("ok install refuses a case-variant existing bundle without creating a duplicate")


def existing_case_variant_file_refuses_install() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-file-install-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        destination = env.vst3_dir / "Plugin.dll"
        existing = env.vst3_dir / "plugin.dll"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old")
        source = base / "Plugin.dll"
        source.write_bytes(b"new")
        plan = Plan(msi=base / "fixture.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(source, destination, "VST3DIR")])

        rows = apply_plan(plan, env)

        assert not destination.exists(), rows
        assert existing.read_bytes() == b"old", rows
        assert any(status == "failed" for status, _path, _note in rows), rows
    print("ok install refuses a case-variant existing file without creating a duplicate")


def symlinked_bundle_is_not_verified_or_queued_for_repair() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-casefold-symlink-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        relative = Path("Contents/x86_64-win/Plugin.vst3")
        destination = env.vst3_dir / "Plugin.vst3"
        backup = env.drive_c / "users/tester/Documents/MyBackup/Plugin.vst3"
        backup_file = backup / relative
        backup_file.parent.mkdir(parents=True)
        backup_file.write_bytes(b"DATA")
        destination.parent.mkdir(parents=True)
        destination.symlink_to(backup, target_is_directory=True)
        source = base / "payload/Plugin.vst3"
        (source / relative).parent.mkdir(parents=True)
        (source / relative).write_bytes(b"DATA")
        plan = Plan(
            msi=base / "fixture.msi",
            identity=MsiIdentity(product_name="Plugin"),
            actions=[Action(source, destination, "VST3DIR")],
            owned_files={destination / relative: MsiFileEntry(
                "F", "VST3DIR", Path("Plugin.vst3") / relative, 4)},
        )

        rows = verify_plan(plan, env)
        assert rows and all(status != "ok" for status, _path, _note in rows), rows
        assert any(status == "ambiguous" and "symlink" in note.lower()
                   for status, _path, note in rows), rows
        try:
            filter_needing_repair(plan, env)
        except MsiError:
            pass
        else:
            raise AssertionError("repair treated a symlinked backup as a verified plugin")
        assert backup_file.read_bytes() == b"DATA"
    print("ok a symlinked backup is neither verified nor queued as a repair target")


def prefix_environment_is_required_for_verification_and_repair() -> None:
    plan = Plan(msi=Path("fixture.msi"), identity=MsiIdentity(product_name="Plugin"))
    for operation in (verify_plan, filter_needing_repair):
        try:
            operation(plan)
        except TypeError:
            continue
        raise AssertionError(f"{operation.__name__} allowed path handling without a prefix environment")
    print("ok verification and repair require prefix context")


if __name__ == "__main__":
    one_case_variant_is_the_same_file()
    two_case_variants_are_ambiguous()
    existing_case_variant_bundle_refuses_install()
    existing_case_variant_file_refuses_install()
    symlinked_bundle_is_not_verified_or_queued_for_repair()
    prefix_environment_is_required_for_verification_and_repair()
    print("casefold manifest check: 6/6 passed")
