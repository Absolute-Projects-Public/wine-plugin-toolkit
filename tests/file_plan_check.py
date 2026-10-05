#!/usr/bin/env python3
"""A plan maps each MSI File row to one exact destination beneath selected payload roots."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import installer, msi  # noqa: E402
from wpt.environment import Environment  # noqa: E402


def manifest_is_mapped_to_exact_destination() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-plan-files-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        root = base / "extract"
        paths = {
            "VST3DIR": Path("Plugin.vst3/Contents/x64/Module.dll"),
            "PREDIR": Path("Artists/Factory.xml"),
        }
        for section, relative in paths.items():
            target = root / section / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(b"fixture")
        ident = msi.MsiIdentity(product_name="Plugin", manufacturer="Neural DSP")
        records = [msi.MsiFileEntry("module", "VST3DIR", paths["VST3DIR"], 7),
                   msi.MsiFileEntry("factory", "PREDIR", paths["PREDIR"], 7)]
        with (patch.object(msi, "identity", return_value=ident),
              patch.object(msi, "expected_sizes", return_value={"Module.dll": 7, "Factory.xml": 7}),
              patch.object(msi, "declares_plugin_payload", return_value=True),
              patch.object(msi, "file_manifest", return_value=records)):
            plan = installer.build_plan(base / "fixture.msi", env, root)
        assert {p: e.size for p, e in plan.owned_files.items()} == {
            env.vst3_dir / paths["VST3DIR"]: 7,
            env.program_data / "Neural DSP" / "Plugin" / paths["PREDIR"]: 7,
        }, plan.owned_files
    print("ok two File rows map to distinct destination paths, including a bundle internal")


def mixed_case_directory_key_still_maps_manifest_rows() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-plan-case-root-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        root = base / "extract"
        relative = Path("Plugin.vst3/Contents/x64/Module.dll")
        source = root / "VST3DIR" / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fixture")
        ident = msi.MsiIdentity(product_name="Plugin", manufacturer="Neural DSP")
        record = msi.MsiFileEntry("module", "vst3dir", relative, 7)
        with (patch.object(msi, "identity", return_value=ident),
              patch.object(msi, "expected_sizes", return_value={"Module.dll": 7}),
              patch.object(msi, "declares_plugin_payload", return_value=True),
              patch.object(msi, "file_manifest", return_value=[record])):
            plan = installer.build_plan(base / "fixture.msi", env, root)
        expected = env.vst3_dir / relative
        assert plan.owned_files == {expected: record}, plan.owned_files
    print("ok mixed-case Directory keys map to the canonical payload destination")


def verify_both_duplicate_basename_files() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-verify-files-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        bundle = base / "prefix" / "drive_c" / "VST3" / "Plugin.vst3"
        a = bundle / "Contents" / "A" / "Plugin.ico"
        b = bundle / "Contents" / "B" / "Plugin.ico"
        for path, content in ((a, b"AAAA"), (b, b"BBBBBBBBB")):
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        plan = installer.Plan(msi=base / "fixture.msi", identity=msi.MsiIdentity(product_name="Plugin"),
                              actions=[installer.Action(bundle, bundle, "VST3DIR")],
                              expected={"Plugin.ico": 9},
                              owned_files={
                                  a: msi.MsiFileEntry("A", "VST3DIR", Path("Plugin.vst3/Contents/A/Plugin.ico"), 4),
                                  b: msi.MsiFileEntry("B", "VST3DIR", Path("Plugin.vst3/Contents/B/Plugin.ico"), 9),
                              })
        rows = installer.verify_plan(plan, env)
        assert {(status, Path(path)) for status, path, _note in rows} == {
            ("ok", a), ("ok", b)
        }, rows
    print("ok verification checks both identically named bundle files against their own size")


def repair_selects_only_the_corrupt_nested_file() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-repair-files-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        source = base / "payload" / "Plugin.vst3"
        target = base / "prefix" / "drive_c" / "VST3" / "Plugin.vst3"
        a_rel = Path("Contents/A/Plugin.ico")
        b_rel = Path("Contents/B/Plugin.ico")
        for relative, content in ((a_rel, b"AAAA"), (b_rel, b"BBBBBBBBB")):
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            (source / relative).write_bytes(content)
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            (target / relative).write_bytes(b"wrong" if relative == a_rel else content)
        a, b = target / a_rel, target / b_rel
        plan = installer.Plan(msi=base / "fixture.msi", identity=msi.MsiIdentity(product_name="Plugin"),
                              actions=[installer.Action(source, target, "VST3DIR")],
                              expected={"Plugin.ico": 9},
                              owned_files={
                                  a: msi.MsiFileEntry("A", "VST3DIR", Path("Plugin.vst3") / a_rel, 4),
                                  b: msi.MsiFileEntry("B", "VST3DIR", Path("Plugin.vst3") / b_rel, 9),
                              })
        repair = installer.filter_needing_repair(plan, env)
        assert [(action.source, action.dest) for action in repair.actions] == [
            (source / a_rel, a)
        ], repair.actions
        assert list(repair.owned_files) == [a], repair.owned_files
    print("ok repair selects only the wrong-sized inner file, not the intact sibling")


def planner_rejects_actions_without_file_rows() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-plan-no-files-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        root = base / "extract"
        (root / "VST3DIR").mkdir(parents=True)
        (root / "VST3DIR" / "Plugin.vst3").write_bytes(b"payload")
        with (patch.object(msi, "identity", return_value=msi.MsiIdentity(product_name="Plugin")),
              patch.object(msi, "expected_sizes", return_value={}),
              patch.object(msi, "declares_plugin_payload", return_value=True),
              patch.object(msi, "file_manifest", return_value=[])):
            try:
                installer.build_plan(base / "fixture.msi", env, root)
            except msi.MsiError as exc:
                assert "manifest" in str(exc).lower(), str(exc)
            else:
                raise AssertionError("planner built a copy action with no File-table identity")
    print("ok planner refuses copy/removal actions with no exact File-table manifest")


def extracted_payload_must_equal_file_table() -> None:
    for problem in ("extra", "missing"):
        with tempfile.TemporaryDirectory(prefix="wpt-payload-manifest-") as tmp:
            base = Path(tmp)
            env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                              wine_tree=base / "tree")
            root = base / "extract" / "VST3DIR"
            contents = root / "Plugin.vst3" / "Contents"
            contents.mkdir(parents=True)
            (contents / "Module.dll").write_bytes(b"payload")
            declared = [msi.MsiFileEntry("F", "VST3DIR",
                                         Path("Plugin.vst3/Contents/Module.dll"), 7)]
            if problem == "extra":
                (contents / "Unexpected.dll").write_bytes(b"surprise")
            else:
                declared.append(msi.MsiFileEntry("G", "VST3DIR",
                                                  Path("Plugin.vst3/Contents/Missing.dll"), 5))
            with (patch.object(msi, "identity", return_value=msi.MsiIdentity(product_name="Plugin")),
                  patch.object(msi, "expected_sizes", return_value={}),
                  patch.object(msi, "declares_plugin_payload", return_value=True),
                  patch.object(msi, "file_manifest", return_value=declared)):
                try:
                    installer.build_plan(base / "fixture.msi", env, root.parent)
                except msi.MsiError as exc:
                    assert "payload" in str(exc).lower(), str(exc)
                else:
                    raise AssertionError(f"accepted {problem} file absent from one side of manifest")
    print("ok extracted payload cannot contain undeclared files or omit declared ones")


def zero_byte_file_is_checked_and_repaired() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-zero-byte-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        dest = base / "prefix/drive_c/Zero.bin"
        source = base / "source/Zero.bin"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")
        plan = installer.Plan(msi=base / "fixture.msi", identity=msi.MsiIdentity(product_name="Zero"),
                              actions=[installer.Action(source, dest, "APPDIR")],
                              owned_files={dest: msi.MsiFileEntry("F", "APPDIR", Path("Zero.bin"), 0)})
        assert installer.verify_plan(plan, env)[0][0] == "missing"
        assert [a.dest for a in installer.filter_needing_repair(plan, env).actions] == [dest]
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"")
        assert installer.verify_plan(plan, env)[0][0] == "ok"
        assert installer.filter_needing_repair(plan, env).actions == []
        dest.write_bytes(b"not empty")
        assert installer.verify_plan(plan, env)[0][0] == "size-mismatch"
        assert [a.dest for a in installer.filter_needing_repair(plan, env).actions] == [dest]
    print("ok a zero-byte File row is not mistaken for no expected size")


def extracted_bytes_must_match_file_table_size() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-source-size-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        root = base / "extract" / "VST3DIR"
        root.mkdir(parents=True)
        (root / "Plugin.vst3").write_bytes(b"wrong")
        declared = [msi.MsiFileEntry("F", "VST3DIR", Path("Plugin.vst3"), 7)]
        with (patch.object(msi, "identity", return_value=msi.MsiIdentity(product_name="Plugin")),
              patch.object(msi, "expected_sizes", return_value={}),
              patch.object(msi, "declares_plugin_payload", return_value=True),
              patch.object(msi, "file_manifest", return_value=declared)):
            try:
                installer.build_plan(base / "fixture.msi", env, root.parent)
            except msi.MsiError as exc:
                assert "size" in str(exc).lower(), str(exc)
            else:
                raise AssertionError("accepted extracted bytes contrary to the File table")
    print("ok extraction size mismatch refuses an install before copying")


if __name__ == "__main__":
    manifest_is_mapped_to_exact_destination()
    mixed_case_directory_key_still_maps_manifest_rows()
    verify_both_duplicate_basename_files()
    repair_selects_only_the_corrupt_nested_file()
    planner_rejects_actions_without_file_rows()
    extracted_payload_must_equal_file_table()
    zero_byte_file_is_checked_and_repaired()
    extracted_bytes_must_match_file_table_size()
    print("file plan check: 7/7 passed")
