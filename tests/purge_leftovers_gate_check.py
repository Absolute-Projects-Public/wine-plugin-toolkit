#!/usr/bin/env python3
"""A remaining MSI-owned file must block registry purge, even without removal errors."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import cli, msi  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.installer import Action, Plan  # noqa: E402


def purge_is_gated_on_remaining_files() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-purge-leftovers-") as tmp:
        base = Path(tmp)
        env = Environment(home=base / "home", prefix=base / "prefix", user="tester",
                          wine_tree=base / "tree")
        fake_msi = base / "Plugin.msi"
        fake_msi.write_bytes(b"fixture")
        dest = env.vst3_dir / "Plugin.vst3"
        remaining = dest / "plugin.dll"
        plan = Plan(msi=fake_msi, identity=msi.MsiIdentity(product_name="Plugin"),
                    actions=[Action(dest, dest, "VST3DIR")])
        args = SimpleNamespace(msi=str(fake_msi), product=None, product_code="{fixture}",
                               scratch=str(base / "scratch"), no_files=False, files_only=True,
                               rescue_presets=False, purge=True, dry_run=False,
                               vendor="Vendor", home=None, prefix=None, tree=None, user=None)
        edit = SimpleNamespace(hive="HKLM", key="Software\\Plugin", value="Path",
                               reason="fixture")
        output = io.StringIO()
        errors = io.StringIO()
        with (patch.object(cli, "_env", return_value=env),
              patch.object(cli, "_pick_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "stage_msi", return_value=fake_msi),
              patch.object(cli.msi_mod, "extract", return_value=None),
              patch.object(cli, "build_plan", return_value=plan),
              patch.object(cli.scan_mod, "is_registered", return_value=False),
              patch.object(cli.installer_mod, "remove_files", return_value=[]),
              patch.object(cli.installer_mod, "leftovers", return_value=[remaining]),
              patch.object(cli.installer_mod, "stale_registry_edits", return_value=[edit]),
              patch.object(cli.installer_mod, "purge_registry") as purge,
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            result = cli.cmd_uninstall(args)

        assert result != 0, output.getvalue()
        assert not purge.called, "registry purge ran while an MSI-owned file remained"
        assert "purge skipped" in errors.getvalue(), errors.getvalue()

    print("ok registry purge is blocked when MSI-owned files remain")


if __name__ == "__main__":
    purge_is_gated_on_remaining_files()
    print("purge leftovers gate check: 1/1 passed")
