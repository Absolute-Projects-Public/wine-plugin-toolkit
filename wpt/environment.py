"""Environment discovery for the ableton-linux Wine stack.

Everything the rest of the tool needs to know about *where* things are lives here:
the Wine tree, the shared prefix, the Windows user inside it, and the standard
plugin directories. No hardcoded versions -- the newest staged tree wins.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV_NAMES = {
    "HOME", "PATH", "WINE", "WINEPREFIX", "WINESERVER", "WINEDEBUG", "WINELOADER",
    "WINEDLLPATH", "WINEARCH", "WINEDLLOVERRIDES", "WINELOADERNOEXEC",
    "WINEPRELOADRESERVE", "WINEUSERNAME", "WINEHOMEDIR", "LD_PRELOAD", "LD_AUDIT",
}
_VALUE_LINEBREAKS = {0, 10, 11, 12, 13, 28, 29, 30, 133, 0x2028, 0x2029}


def validate_env_overrides(values: Mapping[str, str]) -> dict[str, str]:
    """Validate profile overrides without allowing them to replace the selected Wine runtime."""
    if not isinstance(values, Mapping):
        raise ValueError("environment overrides must be an object of NAME: value pairs")
    result: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if name.upper() in _RESERVED_ENV_NAMES:
            raise ValueError(f"environment variable {name} is reserved by the selected Wine stack")
        if not isinstance(value, str) or any(ord(char) in _VALUE_LINEBREAKS for char in value):
            raise ValueError(f"environment value for {name} must be a single-line string without line separators")
        result[name] = value
    return result


TREE_GLOB = "wine-d2d1-nspa-*"
DEFAULT_PREFIX = "~/.wine-ableton"
DEFAULT_USER = "user"       # last resort only: see windows_user()
SKIP_USERS = {"Public", "Default", "Default User", "All Users", "defaultuser0", "Public User"}


class EnvironmentError_(RuntimeError):
    """Raised when the Wine stack cannot be located."""


def _version_key(path: Path) -> list[int]:
    """Sort key for names like wine-d2d1-nspa-11.13 -> [11, 13]."""
    match = re.search(r"-(\d+(?:\.\d+)*)$", path.name)
    return [int(part) for part in match.group(1).split(".")] if match else [0]


def find_wine_trees(home: Path) -> list[Path]:
    """Every usable staged Wine tree under ~/.local/opt, oldest first."""
    opt = home / ".local" / "opt"
    if not opt.is_dir():
        return []
    trees = [p for p in opt.glob(TREE_GLOB) if (p / "bin" / "wine").is_file()]
    return sorted(trees, key=_version_key)


def newest_wine_tree(home: Path) -> Path:
    trees = find_wine_trees(home)
    if not trees:
        raise EnvironmentError_(
            f"no Wine tree matching {TREE_GLOB} under {home / '.local/opt'} "
            "(expected a staged build from shibco/ableton-linux)"
        )
    return trees[-1]


def windows_user(drive_c: Path, override: str | None = None) -> str:
    """The Windows user inside the prefix -- the folder the installers write to.

    Detected, in this order: an explicit override, the prefix's own `drive_c/users/*` (the one
    with an AppData directory), then `$USER`, then a plain "user".

    This used to fall back to a hardcoded name, harmless here, wrong for everyone else and not
    something to publish.
    """
    if override:
        return override
    users = drive_c / "users"
    if users.is_dir():
        candidates = [
            p.name
            for p in sorted(users.iterdir())
            if p.is_dir() and p.name not in SKIP_USERS and (p / "AppData").is_dir()
        ]
        if candidates:
            return candidates[0]
    return os.environ.get("USER") or os.environ.get("LOGNAME") or DEFAULT_USER


@dataclass(frozen=True)
class Environment:
    home: Path
    wine_tree: Path
    prefix: Path
    user: str
    env_overrides: Mapping[str, str] = field(default_factory=dict, repr=False)
    profile_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "env_overrides", validate_env_overrides(self.env_overrides))

    # ---- derived paths -------------------------------------------------
    @property
    def drive_c(self) -> Path:
        return self.prefix / "drive_c"

    @property
    def program_files(self) -> Path:
        return self.drive_c / "Program Files"

    @property
    def program_data(self) -> Path:
        return self.drive_c / "ProgramData"

    @property
    def vst3_dir(self) -> Path:
        return self.program_files / "Common Files" / "VST3"

    @property
    def vst2_dir(self) -> Path:
        return self.program_files / "VstPlugins"

    @property
    def aax_dir(self) -> Path:
        return self.program_files / "Common Files" / "Avid" / "Audio" / "Plug-Ins"

    @property
    def appdata_roaming(self) -> Path:
        return self.drive_c / "users" / self.user / "AppData" / "Roaming"

    @property
    def wine_binary(self) -> Path:
        return self.wine_tree / "bin" / "wine"

    def wine_env(self, *, include_profile_overrides: bool = False) -> dict[str, str]:
        """Build a pinned Wine environment; profile overrides are opt-in for standalone launches."""
        env = dict(os.environ)
        if include_profile_overrides:
            env.update(self.env_overrides)
        env["WINEPREFIX"] = str(self.prefix)
        env["PATH"] = f"{self.wine_tree / 'bin'}:{env.get('PATH', '')}"
        env["WINESERVER"] = str(self.wine_tree / "bin" / "wineserver")
        env.setdefault("WINEDEBUG", "-all")
        # keep a session's D3D knobs if they were exported, else leave alone
        return env

    def describe(self) -> dict[str, str]:
        import platform

        from . import __version__

        return {
            "wpt": __version__,
            "python": platform.python_version(),
            **({"profile": self.profile_name} if self.profile_name else {}),
            "home": str(self.home),
            "wine_tree": str(self.wine_tree),
            "prefix": str(self.prefix),
            "windows_user": self.user,
            "drive_c": str(self.drive_c),
            "vst3_dir": str(self.vst3_dir),
            "vst2_dir": str(self.vst2_dir),
            "aax_dir": str(self.aax_dir),
            "appdata_roaming": str(self.appdata_roaming),
        }


def detect(
    home: str | Path | None = None,
    prefix: str | Path | None = None,
    wine_tree: str | Path | None = None,
    user: str | None = None,
    env_overrides: Mapping[str, str] | None = None,
    profile_name: str | None = None,
) -> Environment:
    """Locate the stack. Any argument left as None is auto-detected."""
    home_path = Path(home).expanduser() if home else Path.home()

    tree = Path(wine_tree).expanduser() if wine_tree else newest_wine_tree(home_path)

    if prefix:
        prefix_path = Path(prefix).expanduser()
    else:
        prefix_path = Path(os.environ.get("WINEPREFIX") or DEFAULT_PREFIX).expanduser()

    if not prefix_path.is_dir():
        raise EnvironmentError_(f"Wine prefix not found: {prefix_path}")
    if not (prefix_path / "drive_c").is_dir():
        raise EnvironmentError_(f"{prefix_path} is not an initialised Wine prefix (no drive_c)")
    if not (tree / "bin" / "wine").is_file():
        raise EnvironmentError_(f"no wine binary at {tree / 'bin' / 'wine'}")

    return Environment(
        home=home_path,
        wine_tree=tree,
        prefix=prefix_path,
        user=windows_user(prefix_path / "drive_c", user),
        env_overrides=env_overrides or {},
        profile_name=profile_name,
    )


def to_windows_path(env: Environment, path: Path) -> str:
    """Translate an in-prefix Linux path to the C:\\ form Wine/msiexec expects."""
    rel = path.relative_to(env.drive_c)
    return "C:\\" + str(rel).replace("/", "\\")