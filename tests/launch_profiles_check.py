#!/usr/bin/env python3
"""Saved launch profiles stay isolated, validated, and pinned to their selected Wine stack."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("WPT_TREE", str(Path(__file__).resolve().parents[1])))

from wpt.environment import EnvironmentError_
from wpt.launch_profiles import (
    LaunchProfile,
    ProfileConfigError,
    ProfileStore,
    format_environment_text,
    load_store,
    parse_environment_text,
    profile_config_path,
    resolve_profile,
    save_store,
)

checks = 0
failures = 0


def check(name, actual, expected):
    global checks, failures
    checks += 1
    if actual == expected:
        print(f"  ok  {name}")
    else:
        failures += 1
        print(f"FAIL {name}: got {actual!r}, expected {expected!r}")


with tempfile.TemporaryDirectory(prefix="wpt-launch-profiles-") as tmp:
    root = Path(tmp)
    home = root / "home"
    config_home = home / ".config"
    config_home.mkdir(parents=True)
    config_file = config_home / "wpt" / "launch_profiles.json"
    tree = home / ".local/opt/wine-d2d1-nspa-test"
    prefix = home / ".wine-mantra"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin/wine").write_text("#!/bin/sh\nexit 0\n")
    (tree / "bin/wine").chmod(0o755)
    (tree / "bin/wineserver").write_text("#!/bin/sh\nexit 0\n")
    (tree / "bin/wineserver").chmod(0o755)
    (prefix / "drive_c").mkdir(parents=True)

    print("1. Profile configuration round-trips without overwriting UI preferences")
    profile = LaunchProfile(
        name="Mantra test",
        prefix=str(prefix),
        wine_tree=str(tree),
        environment={"PIPEWIRE_LATENCY": "128/48000", "LD_LIBRARY_PATH": "/opt/custom/lib"},
    )
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
        check("profile path follows XDG_CONFIG_HOME", profile_config_path(home), config_file)
        store = ProfileStore(profiles=(profile,), active="Mantra test")
        save_store(store, home=home)
        loaded = load_store(home=home)
        check("saved profile round-trips", loaded.profiles, (profile,))
        check("active profile round-trips", loaded.active, "Mantra test")
        original_id = getattr(profile, "profile_id", None)
        loaded_id = getattr(loaded.profiles[0], "profile_id", None)
        check("stable profile identity survives save and load",
              isinstance(original_id, str) and bool(original_id) and loaded_id == original_id, True)
        check("config file is private", config_file.stat().st_mode & 0o777, 0o600)
        lock_file = config_file.with_name(f".{config_file.name}.lock")
        check("concurrent-writer lock file is private", lock_file.stat().st_mode & 0o777, 0o600)

    print("2. Profile resolution pins the selected Wine stack and passes safe overrides")
    with patch.dict(os.environ, {
        "WINEPREFIX": str(root / "wrong-prefix"),
        "WINESERVER": str(root / "wrong-server"),
        "PATH": "/usr/bin",
        "LD_LIBRARY_PATH": "/host/lib",
        "PIPEWIRE_LATENCY": "host-default",
    }):
        env = resolve_profile(profile, home=home)
        child = env.wine_env()
        check("selected prefix is active", env.prefix, prefix)
        check("selected Wine tree is active", env.wine_tree, tree)
        check("profile name is visible", env.profile_name, "Mantra test")
        check("Wine prefix cannot be overridden", child["WINEPREFIX"], str(prefix))
        check("Wine server cannot be overridden", child["WINESERVER"], str(tree / "bin/wineserver"))
        check("custom Wine binary stays first on PATH", child["PATH"].split(":", 1)[0], str(tree / "bin"))
        check("installer Wine env keeps host latency", child["PIPEWIRE_LATENCY"], "host-default")
        check("installer Wine env keeps host library path", child["LD_LIBRARY_PATH"], "/host/lib")
        try:
            standalone_child = env.wine_env(include_profile_overrides=True)
        except TypeError:
            standalone_child = {}
        check("standalone Wine env gets profile override", standalone_child.get("PIPEWIRE_LATENCY"), "128/48000")
        check("standalone may select private Wine libraries",
              standalone_child.get("LD_LIBRARY_PATH"), "/opt/custom/lib")

    print("3. Unsafe or malformed profile environment overrides are rejected")
    for key in ("HOME", "PATH", "WINEPREFIX", "WINESERVER", "WINELOADER", "WINEDLLPATH",
                "WINEARCH", "WINEDLLOVERRIDES", "WINEDEBUG", "LD_PRELOAD", "LD_AUDIT",
                "WINEUSERNAME", "WINEHOMEDIR", "wineprefix"):
        try:
            LaunchProfile("bad", str(prefix), str(tree), {key: "x"})
        except ProfileConfigError:
            rejected = True
        else:
            rejected = False
        check(f"reserved variable {key} is rejected", rejected, True)
    for value in ("line1\nline2", "line1\rline2", "nul\0byte"):
        try:
            LaunchProfile("bad", str(prefix), str(tree), {"PIPEWIRE_LATENCY": value})
        except ProfileConfigError:
            rejected = True
        else:
            rejected = False
        check("multiline or NUL value is rejected", rejected, True)
    try:
        LaunchProfile("bad-id", str(prefix), str(tree), {}, profile_id="")
    except ProfileConfigError:
        rejected = True
    else:
        rejected = False
    check("an explicitly empty profile ID is rejected", rejected, True)

    print("4. Environment text round-trips literal values and rejects line-separator injection")
    literal = {"http_proxy": "http://proxy.test", "PLUGIN_LABEL": "  padded value  "}
    check("editor keeps mixed-case/lowercase names and surrounding value spaces",
          parse_environment_text(format_environment_text(literal)), literal)
    try:
        parsed = parse_environment_text("PLUGIN_NOTE=first\u2028WINEUSERNAME=other")
        LaunchProfile("bad-separator", str(prefix), str(tree), parsed)
    except ProfileConfigError:
        rejected = True
    else:
        rejected = False
    check("Unicode line separator cannot inject another variable", rejected, True)

    print("5. A stale profile window cannot overwrite a newer save")
    conflict_home = home / "conflict-config"
    conflict_home.mkdir()
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(conflict_home)}):
        save_store(ProfileStore((profile,), active="Mantra test"), home=home)
        first = load_store(home=home)
        second = load_store(home=home)
        save_store(replace(second, active=None), home=home)
        try:
            save_store(replace(first, active="Mantra test"), home=home)
        except ProfileConfigError:
            rejected = True
        else:
            rejected = False
        check("stale in-memory store is rejected", rejected, True)
        check("newer profile selection survives conflict", load_store(home=home).active, None)

    print("6. Invalid profile files fail closed and do not silently select another stack")
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
        config_file.write_text('{"version": 1, "profiles": "not-a-list", "active": "Mantra test"}')
        try:
            load_store(home=home)
        except ProfileConfigError:
            rejected = True
        else:
            rejected = False
        check("invalid schema is rejected", rejected, True)

    print("7. Missing selected prefix or Wine tree cannot resolve")
    bad_profile = LaunchProfile("missing", str(root / "absent-prefix"), str(tree), {})
    try:
        resolve_profile(bad_profile, home=home)
    except EnvironmentError_:
        rejected = True
    else:
        rejected = False
    check("missing prefix is rejected", rejected, True)

print(f"launch profile checks: {checks - failures}/{checks} passed")
raise SystemExit(1 if failures else 0)
