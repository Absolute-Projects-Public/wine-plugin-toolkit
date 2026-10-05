#!/usr/bin/env python3
"""Race a prefix-parent symlink swap against direct removal; outside data must survive."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import installer  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan  # noqa: E402
from wpt.msi import MsiFileEntry, MsiIdentity  # noqa: E402


def main() -> int:
    symlinked_home_ancestor_is_supported()
    parent_symlink_swap_cannot_redirect_unlink()
    print("uninstall race safety check: 2/2 passed")
    return 0


def symlinked_home_ancestor_is_supported() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-symlinked-home-") as tmp:
        root = Path(tmp)
        real_home = root / "real-home"
        real_home.mkdir()
        home = root / "home-link"
        home.symlink_to(real_home, target_is_directory=True)
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        directory = env.vst3_dir
        directory.mkdir(parents=True)
        fd = installer._open_existing_directory_no_follow(directory, env)
        try:
            assert Path(f"/proc/self/fd/{fd}").resolve() == directory.resolve()
        finally:
            import os
            os.close(fd)
    print("ok a symlinked host HOME ancestor is allowed while prefix descendants remain no-follow")


def parent_symlink_swap_cannot_redirect_unlink() -> None:
    payload_bytes = b"known MSI payload"
    with tempfile.TemporaryDirectory(prefix="wpt-unlink-race-") as tmp:
        root = Path(tmp)
        home = root / "home"
        prefix = home / ".wine"
        env = Environment(home=home, prefix=prefix, user="tester", wine_tree=root / "tree")
        target = env.vst3_dir / "Race.vst3"
        target.parent.mkdir(parents=True)
        target.write_bytes(payload_bytes)
        outside = root / "outside"
        outside.mkdir()
        outside_file = outside / target.name
        outside_file.write_bytes(payload_bytes)
        backup_parent = target.parent.with_name(target.parent.name + ".original")
        source = root / "payload.vst3"
        source.write_bytes(payload_bytes)
        plan = Plan(
            msi=root / "Product.msi",
            identity=MsiIdentity(product_name="Race test"),
            actions=[Action(source, target, "VST3DIR")],
            owned_files={target: MsiFileEntry(target.name, "VST3DIR", Path(target.name), len(payload_bytes))},
        )

        real_compare = installer._same_file_contents
        swapped = False

        def swap_parent_after_verified_compare(left: Path, right) -> bool:
            nonlocal swapped
            matches = real_compare(left, right)
            if matches and not swapped:
                target.parent.rename(backup_parent)
                target.parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            return matches

        with patch.object(installer, "_same_file_contents", side_effect=swap_parent_after_verified_compare):
            rows = installer.remove_files(plan, env)

        assert swapped, "the test did not hit the post-verification race window"
        assert outside_file.exists(), "removal followed a raced parent symlink and deleted outside data"
        assert outside_file.read_bytes() == payload_bytes
        assert not (backup_parent / target.name).exists(), "the in-prefix target was not unlinked via its pinned parent"
        assert any(row[0] == "removed" for row in rows), rows
    print("ok parent-symlink swap cannot redirect File-table unlink outside the prefix")


if __name__ == "__main__":
    raise SystemExit(main())
