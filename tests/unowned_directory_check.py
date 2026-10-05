#!/usr/bin/env python3
"""Uninstall must not delete user-added files inside a planned bundle directory.

Everything is under TemporaryDirectory. The extracted source is the set of files the vendor MSI
provided; the installed destination also has a user-created file absent from that source.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan, leftovers, remove_files  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def extracted_bundle_preserves_unowned_files() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-bundle-safety-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "VST3DIR" / "Product.vst3"
        target = env.vst3_dir / "Product.vst3"
        owned = [Path("Contents/x64/Module.dll"), Path("Contents/Resources/Factory.xml")]
        for relative in owned:
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            (source / relative).write_bytes(b"FACTORY")
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            (target / relative).write_bytes(b"FACTORY")
        personal = target / "User" / "My Sound.xml"
        personal.parent.mkdir(parents=True, exist_ok=True)
        personal.write_text("not in the MSI")
        plan = Plan(msi=base / "Product.msi", identity=MsiIdentity(product_name="Product"),
                    expected={p.name: 7 for p in owned},
                    actions=[Action(source=source, dest=target, label="VST3DIR")],
                    owned_files={target / p: MsiFileEntry(p.name, "VST3DIR", Path("Product.vst3") / p, 7)
                                 for p in owned})

        preview = remove_files(plan, env, dry_run=True)
        assert personal.is_file() and all((target / p).is_file() for p in owned)
        assert {Path(path) for state, path, _ in preview if state == "dry-run"} == {
            target / p for p in owned
        }, preview
        rows = remove_files(plan, env)
        assert personal.read_text() == "not in the MSI", rows
        assert all(not (target / p).exists() for p in owned), rows
        assert target.is_dir(), rows
        assert leftovers(plan, env) == [], leftovers(plan, env)
    print("ok bundle removal preserves unowned user file and reports no owned leftovers")


def missing_manifest_refuses_deletion() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-manifest-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        bundle = env.vst3_dir / "Mystery.vst3"
        personal = bundle / "User" / "My Sound.xml"
        personal.parent.mkdir(parents=True)
        personal.write_text("keep this")
        plan = Plan(msi=base / "unknown.msi", identity=MsiIdentity(product_name="Mystery"),
                    actions=[Action(bundle, bundle, "VST3DIR")])
        rows = remove_files(plan, env)
        assert personal.read_text() == "keep this", rows
        assert any(status == "refused" for status, _path, _note in rows), rows
    print("ok a plan with no exact manifest refuses a directory deletion")


def modified_msi_file_is_preserved() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-modified-owned-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        target = env.vst3_dir / "Plugin.vst3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"my modified content")
        plan = Plan(msi=base / "package.msi", identity=MsiIdentity(product_name="Plugin"),
                    actions=[Action(target, target, "VST3DIR")],
                    owned_files={target: MsiFileEntry("F", "VST3DIR", Path(target.name), 4)})
        rows = remove_files(plan, env)
        assert target.read_bytes() == b"my modified content", rows
        assert any(state == "refused" for state, _path, _note in rows), rows
    print("ok an MSI-named file with altered size is preserved for manual review")


if __name__ == "__main__":
    extracted_bundle_preserves_unowned_files()
    missing_manifest_refuses_deletion()
    modified_msi_file_is_preserved()
    print("unowned directory check: 3/3 passed")
