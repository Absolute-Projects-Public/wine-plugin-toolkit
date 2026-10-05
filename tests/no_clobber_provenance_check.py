#!/usr/bin/env python3
"""PREDIR/no-clobber files lack installation provenance, even when bytes match."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import installer, presets  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def no_clobber_files_are_preserved_and_rescued() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-owner-") as tmp:
        root = Path(tmp)
        home = root / "home"
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        product_folder = env.program_data / "Neural DSP" / "Product"
        product_root = product_folder / "User"
        product_root.mkdir(parents=True)
        factory_target = product_folder / "Factory.bin"
        payload = root / "payload"
        payload_user = payload / "User"
        payload_user.mkdir(parents=True)
        names_and_bytes = {
            "Tone.xml": b"<preset>same byte content</preset>",
            "user-state.bin": b"same-size state",
        }
        for name, data in names_and_bytes.items():
            (product_root / name).write_bytes(data)
            (payload_user / name).write_bytes(data)
        factory_bytes = b"factory-sidecar"
        factory_target.write_bytes(factory_bytes)
        factory_source = payload / "Factory.bin"
        factory_source.write_bytes(factory_bytes)
        plan = Plan(
            msi=root / "Product.msi",
            identity=MsiIdentity(product_code="{PRODUCT}", product_name="Product",
                                 manufacturer="Neural DSP"),
            actions=[
                Action(payload_user, product_root, "PREDIR", no_clobber=True),
                Action(factory_source, factory_target, "PREDIR", no_clobber=True),
            ],
            owned_files={
                **{
                    product_root / name: MsiFileEntry(name, "PREDIR", Path(name), len(data))
                    for name, data in names_and_bytes.items()
                },
                factory_target: MsiFileEntry("Factory", "PREDIR", Path("Factory.bin"), len(factory_bytes)),
            },
        )

        removal = installer.remove_files(plan, env)
        assert sum(row[0] == "refused" for row in removal) == len(names_and_bytes) + 1, removal
        assert all(row[0] in {"refused", "kept"} for row in removal), removal
        assert all((product_root / name).read_bytes() == data
                   for name, data in names_and_bytes.items())
        assert factory_target.read_bytes() == factory_bytes
        expected_leftovers = {product_root / name for name in names_and_bytes} | {factory_target}
        assert set(installer.leftovers(plan, env)) == expected_leftovers

        rescue_root = root / "rescue"
        rows, saved_for, _looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=rescue_root, stamp="fixed")
        failed = [row for row in rows if row[0] == "failed"]
        assert not failed, failed
        assert saved_for, (rows, saved_for)
        shutil.rmtree(product_folder)
        expected = {**names_and_bytes, "Factory.bin": factory_bytes}
        for name, data in expected.items():
            matches = list(rescue_root.rglob(name))
            assert matches and any(path.read_bytes() == data for path in matches), (name, matches)

    print("ok no-clobber files stay in place, and every file is rescued before vendor removal")


def no_clobber_repair_preserves_unproven_existing_file() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-repair-") as tmp:
        root = Path(tmp)
        home = root / "home"
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        target = env.program_data / "Neural DSP" / "Product" / "User" / "Tone.xml"
        target.parent.mkdir(parents=True)
        user_bytes = b"custom user preset"
        target.write_bytes(user_bytes)
        payload = root / "Tone.xml"
        payload.write_bytes(b"factory preset from MSI")
        plan = Plan(
            msi=root / "Product.msi",
            identity=MsiIdentity(product_code="{PRODUCT}", product_name="Product",
                                 manufacturer="Neural DSP"),
            actions=[Action(payload, target, "PREDIR", no_clobber=True)],
            owned_files={target: MsiFileEntry("Tone.xml", "PREDIR", Path("Tone.xml"),
                                              payload.stat().st_size)},
        )
        repair = installer.filter_needing_repair(plan, env)
        assert len(repair.actions) == 1 and repair.actions[0].no_clobber
        rows = installer.apply_plan(repair, env)
        assert [row[0] for row in rows] == ["kept"], rows
        assert target.read_bytes() == user_bytes, "repair overwrote a no-clobber user preset"
        assert [row[0] for row in installer.verify_plan(repair, env)] == ["size-mismatch"]
    print("ok repair preserves an existing no-clobber file with unknown provenance")


def symlinked_preset_is_not_copied_outside_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-symlink-") as tmp:
        root = Path(tmp)
        home = root / "home"
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        product_root = env.program_data / "Neural DSP" / "Product" / "User"
        product_root.mkdir(parents=True)
        outside = root / "outside.xml"
        outside.write_bytes(b"must not be copied")
        linked = product_root / "linked.xml"
        linked.symlink_to(outside)
        plan = Plan(
            msi=root / "Product.msi",
            identity=MsiIdentity(product_code="{PRODUCT}", product_name="Product",
                                 manufacturer="Neural DSP"),
            actions=[Action(root / "payload", product_root, "PREDIR", no_clobber=True)],
        )
        rescue_root = root / "rescue"
        rows, _saved_for, _looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=rescue_root, stamp="fixed")
        assert any(row[0] == "failed" and "symlink" in row[2] for row in rows), rows
        assert not any(path.is_file() and path.read_bytes() == outside.read_bytes()
                       for path in rescue_root.rglob("*")), list(rescue_root.rglob("*"))
    print("ok symlinked no-clobber data fails rescue without copying outside content")


def msi_manufacturer_selects_non_default_preset_vendor() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-vendor-") as tmp:
        root = Path(tmp)
        home = root / "home"
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        user_dir = env.program_data / "Acme Audio" / "Amp X" / "User"
        user_dir.mkdir(parents=True)
        preset = user_dir / "My Tone.xml"
        preset.write_bytes(b"<tone>keep me</tone>")
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X",
                                 manufacturer="Acme Audio"),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        rescue_root = root / "rescue"
        rows, saved_for, looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=rescue_root, stamp="fixed")
        assert any(row[0] == "saved" and row[1] == str(preset) for row in rows), rows
        assert "Amp X" in looked_at, looked_at
        assert saved_for, saved_for
        copies = list(rescue_root.rglob("My Tone.xml"))
        assert copies and any(path.read_bytes() == preset.read_bytes() for path in copies), copies
    print("ok MSI manufacturer selects its actual ProgramData preset tree")


def unmatched_no_clobber_tree_fails_rescue_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-unmatched-") as tmp:
        root = Path(tmp)
        home = root / "home"
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        user_dir = env.program_data / "CustomRoot" / "Amp X" / "User"
        user_dir.mkdir(parents=True)
        preset = user_dir / "My Tone.xml"
        preset.write_bytes(b"<tone>keep me</tone>")
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X", manufacturer=""),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        rows, _saved_for, _looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=root / "rescue", stamp="fixed")
        assert any(row[0] == "failed" and str(preset) in row[1] for row in rows), rows
        assert preset.exists(), "the fail-closed check must not alter the original file"
    print("ok unmatched no-clobber data blocks uninstall when it cannot be rescued")


def inaccessible_no_clobber_subtree_fails_rescue_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-skipped-") as tmp:
        root = Path(tmp)
        env = Environment(home=root / "home", prefix=root / "home" / ".wine",
                          user="tester", wine_tree=root / "tree")
        user_dir = env.program_data / "Acme Audio" / "Amp X" / "User"
        hidden = user_dir / "Restricted"
        hidden.mkdir(parents=True)
        protected = hidden / "State.bin"
        protected.write_bytes(b"non-preset no-clobber data")
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X",
                                 manufacturer="Acme Audio"),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        real_scandir = os.scandir
        denied = False

        def deny_hidden_directory(path):
            nonlocal denied
            if Path(path) == hidden:
                denied = True
                raise PermissionError("fixture unreadable directory")
            return real_scandir(path)

        with patch("os.scandir", side_effect=deny_hidden_directory):
            rows, _saved_for, _looked_at = presets.rescue_for_plan(
                env, plan, rescue_root=root / "rescue", stamp="fixed")

        assert denied, "strict rescue traversal never attempted to enumerate the hidden directory"
        assert any(row[0] == "failed" and "enumerate" in row[2] for row in rows), rows
        assert protected.read_bytes() == b"non-preset no-clobber data"
    print("ok inaccessible no-clobber subtree blocks uninstall")


def inaccessible_roaming_midi_tree_fails_rescue_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-roaming-midi-unreadable-") as tmp:
        root = Path(tmp)
        env = Environment(home=root / "home", prefix=root / "home" / ".wine",
                          user="tester", wine_tree=root / "tree")
        user_dir = env.program_data / "Acme Audio" / "Amp X" / "User"
        user_dir.mkdir(parents=True)
        midi_root = env.appdata_roaming / "Acme Audio" / "Amp X" / "MIDI"
        midi_root.mkdir(parents=True)
        protected = midi_root / "map.xml"
        protected.write_bytes(b"<map>user mapping</map>")
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X",
                                 manufacturer="Acme Audio"),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        real_rglob = Path.rglob
        real_scandir = os.scandir

        def hide_midi_rglob(self, pattern):
            if self == midi_root:
                return iter(())
            return real_rglob(self, pattern)

        def deny_midi_scandir(path):
            if Path(path) == midi_root:
                raise PermissionError("fixture unreadable MIDI directory")
            return real_scandir(path)

        with patch.object(Path, "rglob", new=hide_midi_rglob), \
                patch("os.scandir", side_effect=deny_midi_scandir):
            rows, _saved_for, _looked_at = presets.rescue_for_plan(
                env, plan, rescue_root=root / "rescue", stamp="fixed")

        assert any(row[0] == "failed" and "MIDI" in row[2] for row in rows), rows
        assert protected.read_bytes() == b"<map>user mapping</map>"
    print("ok inaccessible roaming MIDI data blocks uninstall")


def roaming_midi_is_rescued_when_generic_scan_misses_it() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-roaming-midi-fallback-") as tmp:
        root = Path(tmp)
        env = Environment(home=root / "home", prefix=root / "home" / ".wine",
                          user="tester", wine_tree=root / "tree")
        user_dir = env.program_data / "Acme Audio" / "Amp X" / "User"
        user_dir.mkdir(parents=True)
        midi_root = env.appdata_roaming / "Acme Audio" / "Amp X" / "MIDI"
        midi_root.mkdir(parents=True)
        mapping = midi_root / "map.xml"
        mapping.write_bytes(b"<map>user mapping</map>")
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X",
                                 manufacturer="Acme Audio"),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        real_rglob = Path.rglob

        def hide_midi_rglob(self, pattern):
            if self == midi_root:
                return iter(())
            return real_rglob(self, pattern)

        rescue_root = root / "rescue"
        with patch.object(Path, "rglob", new=hide_midi_rglob):
            rows, _saved_for, _looked_at = presets.rescue_for_plan(
                env, plan, rescue_root=rescue_root, stamp="fixed")

        assert any(row[0] == "saved" and row[1] == str(mapping) for row in rows), rows
        copies = list(rescue_root.rglob("map.xml"))
        assert copies and any(path.read_bytes() == mapping.read_bytes() for path in copies), copies
    print("ok strict traversal rescues MIDI maps missed by the generic collector")


def malicious_msi_manufacturer_cannot_redirect_rescue_outside_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-rescue-path-escape-") as tmp:
        root = Path(tmp)
        env = Environment(home=root / "home", prefix=root / "home" / ".wine",
                          user="tester", wine_tree=root / "tree")
        outside_vendor = root / "outside-vendor"
        external_user = outside_vendor / "Amp X" / "User"
        external_user.mkdir(parents=True)
        secret = external_user / "Private.xml"
        secret.write_bytes(b"must not be copied from outside the prefix")
        user_dir = env.program_data / "Acme Audio" / "Amp X" / "User"
        user_dir.mkdir(parents=True)
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X",
                                 manufacturer=str(outside_vendor)),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        rescue_root = root / "rescue"
        rows, _saved_for, _looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=rescue_root, stamp="fixed")

        assert any(row[0] == "failed" for row in rows), rows
        assert not rescue_root.exists() or not any(rescue_root.rglob("*")), list(rescue_root.rglob("*"))
        assert secret.read_bytes() == b"must not be copied from outside the prefix"
    print("ok unsafe MSI Manufacturer cannot redirect preset rescue")


def symlinked_product_ancestor_cannot_redirect_rescue_outside_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-no-clobber-parent-link-") as tmp:
        root = Path(tmp)
        env = Environment(home=root / "home", prefix=root / "home" / ".wine",
                          user="tester", wine_tree=root / "tree")
        external_vendor = root / "external" / "Acme Audio"
        external_user = external_vendor / "Amp X" / "User"
        external_user.mkdir(parents=True)
        secret = external_user / "user.xml"
        secret.write_bytes(b"external preset")
        vendor_link = env.program_data / "Acme Audio"
        vendor_link.parent.mkdir(parents=True, exist_ok=True)
        vendor_link.symlink_to(external_vendor, target_is_directory=True)
        user_dir = vendor_link / "Amp X" / "User"
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="Amp X",
                                 manufacturer="Acme Audio"),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        rescue_root = root / "rescue"
        rows, _saved_for, _looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=rescue_root, stamp="fixed")

        assert any(row[0] == "failed" for row in rows), rows
        assert not list(rescue_root.rglob("user.xml")) if rescue_root.exists() else True
    print("ok symlinked product ancestor cannot redirect rescue outside prefix")


def malicious_msi_product_name_cannot_redirect_roaming_rescue() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-rescue-product-escape-") as tmp:
        root = Path(tmp)
        env = Environment(home=root / "home", prefix=root / "home" / ".wine",
                          user="tester", wine_tree=root / "tree")
        external_midi = env.appdata_roaming / "outside-product" / "MIDI"
        external_midi.mkdir(parents=True)
        private_map = external_midi / "Private.xml"
        private_map.write_bytes(b"not the selected product's MIDI data")
        user_dir = env.program_data / "Acme Audio" / "Amp X" / "User"
        user_dir.mkdir(parents=True)
        payload = root / "payload"
        payload.mkdir()
        plan = Plan(
            msi=root / "AmpX.msi",
            identity=MsiIdentity(product_code="{ACME}", product_name="../outside-product",
                                 manufacturer="Acme Audio"),
            actions=[Action(payload, user_dir, "PREDIR", no_clobber=True)],
        )
        rescue_root = root / "rescue"
        rows, _saved_for, _looked_at = presets.rescue_for_plan(
            env, plan, rescue_root=rescue_root, stamp="fixed")

        assert any(row[0] == "failed" for row in rows), rows
        assert not rescue_root.exists() or not any(rescue_root.rglob("*")), list(rescue_root.rglob("*"))
        assert private_map.read_bytes() == b"not the selected product's MIDI data"
    print("ok malformed MSI ProductName cannot redirect roaming MIDI rescue")


if __name__ == "__main__":
    no_clobber_files_are_preserved_and_rescued()
    no_clobber_repair_preserves_unproven_existing_file()
    symlinked_preset_is_not_copied_outside_prefix()
    msi_manufacturer_selects_non_default_preset_vendor()
    unmatched_no_clobber_tree_fails_rescue_closed()
    inaccessible_no_clobber_subtree_fails_rescue_closed()
    inaccessible_roaming_midi_tree_fails_rescue_closed()
    roaming_midi_is_rescued_when_generic_scan_misses_it()
    malicious_msi_manufacturer_cannot_redirect_rescue_outside_prefix()
    malicious_msi_product_name_cannot_redirect_roaming_rescue()
    symlinked_product_ancestor_cannot_redirect_rescue_outside_prefix()
    print("no-clobber provenance check: 11/11 passed")