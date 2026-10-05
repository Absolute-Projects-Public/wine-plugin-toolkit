"""Safe standalone matching and custom-Wine launch contract.

Run: python3 tests/standalone_launch_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt.environment import Environment
from wpt.inventory import Inventory, PluginEntry
from wpt.standalone import StandaloneLaunch, launch_standalone, resolve_standalone

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


def make_env(root: Path) -> Environment:
    home = root / "home"
    wine_tree = home / ".local/opt/wine-d2d1-nspa-11.13"
    (wine_tree / "bin").mkdir(parents=True)
    (wine_tree / "bin/wine").write_text("wine marker")
    (wine_tree / "bin/wineserver").write_text("wineserver marker")
    prefix = root / "prefix"
    (prefix / "drive_c/Program Files").mkdir(parents=True)
    return Environment(home=home, wine_tree=wine_tree, prefix=prefix, user="tester")


def plugin(
    name: str,
    path: Path,
    *,
    msi: Path | None = None,
    integrity: str = "unverified",
    kind: str = "vst3",
) -> PluginEntry:
    entry = PluginEntry(name=name, path=path, kind=kind, size=10, mtime=0.0, msi=msi)
    if integrity == "ok":
        entry.expected_size = 10
    elif integrity == "mismatch":
        entry.expected_size = 99
    return entry


def standalone(
    path: Path,
    *,
    msi: Path | None = None,
    integrity: str = "unverified",
    disabled: bool = False,
) -> PluginEntry:
    entry = PluginEntry(
        name=path.name, path=path, kind="standalone", size=10, mtime=0.0,
        msi=msi, disabled=disabled,
    )
    if integrity == "ok":
        entry.expected_size = 10
    elif integrity == "mismatch":
        entry.expected_size = 99
    return entry


class FakeProcess:
    def __init__(self, pid: int = 4312):
        self.pid = pid
        self.returncode = None
        self.waited = threading.Event()

    def wait(self):
        self.returncode = 0
        self.waited.set()
        return 0

    def poll(self):
        return self.returncode


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wpt-standalone-") as temp:
        root = Path(temp)
        env = make_env(root)
        product_dir = env.program_files / "Neural DSP" / "Archetype Rabea X"
        product_dir.mkdir(parents=True)
        exe = product_dir / "Archetype Rabea X.exe"
        exe.write_bytes(b"0123456789")
        vst3 = env.vst3_dir / "Archetype Rabea X.vst3"
        vst3.parent.mkdir(parents=True)
        vst3.write_bytes(b"0123456789")
        msi = env.program_data / "Package Cache" / "Rabea.msi"
        msi.parent.mkdir(parents=True)
        msi.write_text("MSI marker")
        selected = plugin(vst3.name, vst3, msi=msi, integrity="ok")
        app = standalone(exe, msi=msi, integrity="ok")
        inv = Inventory(entries=[selected, app])

        print("1. Resolve one exact basename and retain the MSI File-table size")
        resolution = resolve_standalone(selected, inv, env)
        check("exact plugin stem resolves", resolution.executable, exe.resolve())
        check("matching MSI ownership is reported", resolution.verified, True)
        check("expected size is available for a launch-time recheck", resolution.expected_size, 10)

        print("2. Disabled plugin names match the base product; disabled apps do not")
        disabled_plugin = plugin(
            "Archetype Rabea X.vst3.disabled", vst3.with_name(vst3.name + ".disabled"), msi=msi
        )
        check(
            "disabled plugin suffix is stripped",
            resolve_standalone(disabled_plugin, inv, env).executable,
            exe.resolve(),
        )
        disabled_app = standalone(exe, disabled=True)
        check(
            "disabled standalone is ignored",
            resolve_standalone(selected, Inventory(entries=[selected, disabled_app]), env).executable,
            None,
        )

        print("3. Missing, near-miss, wrong-kind and ambiguous candidates fail closed")
        missing = plugin("No Standalone.vst3", env.vst3_dir / "No Standalone.vst3")
        check("missing candidate is unavailable", resolve_standalone(missing, inv, env).executable, None)
        near_a = standalone(product_dir / "Archetype Rabea X Demo.exe")
        near_b = standalone(product_dir / "Archetype Rabea.exe")
        near_miss = resolve_standalone(selected, Inventory(entries=[selected, near_a, near_b]), env)
        check("prefix and substring near-misses do not match", near_miss.executable, None)
        wrong_kind = standalone(exe)
        wrong_kind.kind = "other"
        check(
            "non-plugin selection is rejected",
            resolve_standalone(wrong_kind, inv, env).executable,
            None,
        )
        duplicate = product_dir / "archetype rabea x.EXE"
        duplicate.write_bytes(b"second file")
        ambiguous = resolve_standalone(
            selected, Inventory(entries=[selected, app, standalone(duplicate)]), env
        )
        check("case-only duplicate is ambiguous", ambiguous.executable, None)
        check("ambiguity is identified", "multiple" in ambiguous.reason.lower(), True)

        print("4. MSI ownership and integrity are not overstated")
        other_msi = env.program_data / "Other.msi"
        mismatch_owner = standalone(exe, msi=other_msi, integrity="ok")
        check(
            "different MSI owner is unverified",
            resolve_standalone(selected, Inventory(entries=[selected, mismatch_owner]), env).verified,
            False,
        )
        wrong_size = standalone(exe, msi=msi, integrity="mismatch")
        check(
            "MSI size mismatch is unverified",
            resolve_standalone(selected, Inventory(entries=[selected, wrong_size]), env).verified,
            False,
        )

        print("5. Path confinement rejects outside files and symlink aliases")
        outside = root / "outside" / "Archetype Rabea X.exe"
        outside.parent.mkdir()
        outside.write_bytes(b"outside marker")
        direct_outside = resolve_standalone(
            selected, Inventory(entries=[selected, standalone(outside)]), env
        )
        check("same-name executable outside Program Files is refused", direct_outside.executable, None)
        escaped = env.program_files / "Alias" / "Archetype Rabea X.exe"
        escaped.parent.mkdir()
        escaped.symlink_to(outside)
        escaped_resolution = resolve_standalone(
            selected, Inventory(entries=[selected, standalone(escaped)]), env
        )
        check("same-name symlink escaping the prefix is refused", escaped_resolution.executable, None)
        helper = env.program_files / "Helpers" / "Vendor Helper.exe"
        helper.parent.mkdir()
        helper.write_bytes(b"helper")
        alias = env.program_files / "Alias" / "Archetype Rabea X.exe"
        alias.unlink()
        alias.symlink_to(helper)
        alias_resolution = resolve_standalone(
            selected, Inventory(entries=[selected, standalone(alias)]), env
        )
        check("same-name symlink to a different in-prefix app is refused", alias_resolution.executable, None)
        target_dir = env.program_files / "Same Prefix Target"
        target_dir.mkdir()
        target_exe = target_dir / "Archetype Rabea X.exe"
        target_exe.write_bytes(b"same-prefix target")
        alias_dir = env.program_files / "Vendor Alias"
        alias_dir.symlink_to(target_dir, target_is_directory=True)
        ancestor_alias = alias_dir / target_exe.name
        ancestor_resolution = resolve_standalone(
            selected, Inventory(entries=[selected, standalone(ancestor_alias)]), env
        )
        check("symlinked ancestor directory is refused", ancestor_resolution.executable, None)
        dotdot_dir = exe.parent / "ordinary"
        dotdot_dir.mkdir()
        dotdot_path = dotdot_dir / ".." / exe.name
        dotdot_resolution = resolve_standalone(
            selected, Inventory(entries=[selected, standalone(dotdot_path)]), env
        )
        check("path containing dot-dot is refused", dotdot_resolution.executable, None)
        with patch("wpt.standalone.subprocess.Popen") as popen:
            try:
                launch_standalone(env, alias)
            except ValueError:
                rejected_alias = True
            else:
                rejected_alias = False
        check("launcher refuses in-prefix symlink aliases", rejected_alias, True)
        check("rejected alias never starts a process", popen.called, False)

        print("6. Launch uses the selected custom Wine environment and reaps its child")
        fake_process = FakeProcess()
        decoys = {
            "WINEPREFIX": "/wrong/prefix",
            "WINESERVER": "/wrong/wineserver",
            "WINELOADER": "/wrong/wine",
            "WINEDLLPATH": "/wrong/dlls",
            "WINEARCH": "win32",
            "WINEDLLOVERRIDES": "mshtml=native",
            "LD_PRELOAD": "/untrusted/libinject.so",
            "WINELOADERNOEXEC": "1",
            "WINEPRELOADRESERVE": "1",
            "WINEDEBUG": "+relay",
            "LD_LIBRARY_PATH": "/custom/wine-libs",
            "XDG_CACHE_HOME": str(env.home / "xdg-cache"),
        }
        with patch.dict(os.environ, decoys, clear=False):
            with patch("wpt.standalone.subprocess.Popen", return_value=fake_process) as popen:
                launch = launch_standalone(env, exe, expected_size=10)
        check("returns a launch handle", isinstance(launch, StandaloneLaunch), True)
        check("child PID is exposed", launch.pid, 4312)
        check("file size still matches the MSI row", launch.size_matches, True)
        check("child is reaped asynchronously", fake_process.waited.wait(1), True)
        args, kwargs = popen.call_args
        check("program is the selected custom Wine", args[0][0], str(env.wine_binary))
        check("argument is the exact executable path", args[0][1], str(exe.resolve()))
        check("working directory is the app folder", kwargs["cwd"], str(exe.parent.resolve()))
        check("prefix overrides inherited value", kwargs["env"]["WINEPREFIX"], str(env.prefix))
        check("Wine server overrides inherited value", kwargs["env"]["WINESERVER"], str(env.wine_tree / "bin/wineserver"))
        check("custom Wine is first on PATH", kwargs["env"]["PATH"].split(":", 1)[0], str(env.wine_tree / "bin"))
        scrubbed = ("WINELOADER", "WINEDLLPATH", "WINEARCH", "WINEDLLOVERRIDES",
                    "LD_PRELOAD", "WINELOADERNOEXEC", "WINEPRELOADRESERVE")
        check("ambient loader overrides are removed", all(key not in kwargs["env"] for key in scrubbed), True)
        check("Wine debug output is forced quiet", kwargs["env"]["WINEDEBUG"], "-all")
        check("custom Wine library path is preserved", kwargs["env"]["LD_LIBRARY_PATH"], "/custom/wine-libs")
        check("custom XDG cache directory is honored", launch.log_path.parent,
              env.home / "xdg-cache/wpt/standalone")

        files_before = set(launch.log_path.parent.glob("*.log"))
        with patch.dict(os.environ, decoys, clear=False):
            with patch("wpt.standalone.subprocess.Popen", side_effect=OSError("spawn denied")):
                try:
                    launch_standalone(env, exe)
                except OSError:
                    spawn_failed = True
                else:
                    spawn_failed = False
        check("Popen error is surfaced", spawn_failed, True)
        check("failed Popen removes its temporary log", set(launch.log_path.parent.glob("*.log")), files_before)
        check("stdin is detached", kwargs["stdin"], __import__("subprocess").DEVNULL)
        check("stdout is captured in a per-launch log", Path(kwargs["stdout"].name), launch.log_path)
        check("stderr joins the log", kwargs["stderr"], __import__("subprocess").STDOUT)
        check("launch is detached and shell-free",
              (kwargs["shell"], kwargs["start_new_session"]), (False, True))
        check("log directory follows XDG_CACHE_HOME",
              launch.log_path.parent, env.home / "xdg-cache/wpt/standalone")
        check("launch log exists", launch.log_path.is_file(), True)

        fallback_cache = env.home / ".cache/wpt/standalone"
        for label, xdg_cache in (("relative", "relative-cache"), ("empty", "")):
            with patch.dict(os.environ, {"XDG_CACHE_HOME": xdg_cache}):
                with patch("wpt.standalone.subprocess.Popen", return_value=FakeProcess()):
                    fallback_launch = launch_standalone(env, exe)
            check(f"{label} XDG_CACHE_HOME falls back to HOME cache", fallback_launch.log_path.parent,
                  fallback_cache)
            fallback_launch.log_path.unlink(missing_ok=True)
        with patch.dict(os.environ):
            os.environ.pop("XDG_CACHE_HOME", None)
            with patch("wpt.standalone.subprocess.Popen", return_value=FakeProcess()):
                unset_launch = launch_standalone(env, exe)
        check("unset XDG_CACHE_HOME falls back to HOME cache", unset_launch.log_path.parent, fallback_cache)
        unset_launch.log_path.unlink(missing_ok=True)

        print("7. A changed verified file is refused before spawn; missing Wine fails too")
        exe.write_bytes(b"changed bytes, different length")
        with patch("wpt.standalone.subprocess.Popen") as popen:
            try:
                launch_standalone(env, exe, expected_size=10)
            except ValueError as exc:
                changed_refused = "changed size" in str(exc).lower()
            else:
                changed_refused = False
        check("changed verified size is refused", changed_refused, True)
        check("changed verified file is not spawned", popen.called, False)
        env.wine_binary.unlink()
        with patch("wpt.standalone.subprocess.Popen") as popen:
            try:
                launch_standalone(env, exe)
            except FileNotFoundError:
                missing_wine = True
            else:
                missing_wine = False
        check("missing custom Wine raises a clear error", missing_wine, True)
        check("missing Wine does not spawn a child", popen.called, False)

        print("8. An arbitrary executable outside the prefix is never launched")
        with patch("wpt.standalone.subprocess.Popen") as popen:
            try:
                launch_standalone(env, outside)
            except ValueError:
                rejected = True
            else:
                rejected = False
        check("outside executable is rejected", rejected, True)
        check("rejected path never starts a process", popen.called, False)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("standalone launch checks: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())