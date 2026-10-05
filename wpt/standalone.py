"""Safely resolve and launch a plugin's standalone application in its Wine prefix."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from .environment import Environment
from .inventory import DISABLED_SUFFIX, Inventory, PluginEntry


@dataclass(frozen=True)
class StandaloneResolution:
    executable: Path | None
    reason: str = ""
    verified: bool = False
    expected_size: int | None = None


@dataclass(frozen=True)
class StandaloneLaunch:
    """A detached Wine child plus its output log and launch-time size check."""

    process: subprocess.Popen[bytes]
    log_path: Path
    size_matches: bool

    @property
    def pid(self) -> int:
        return self.process.pid


def _stem(name: str) -> str:
    if name.lower().endswith(DISABLED_SUFFIX):
        name = name[: -len(DISABLED_SUFFIX)]
    return Path(name).stem.casefold()


def _inside_program_files(path: Path, env: Environment) -> Path | None:
    """Resolve an existing non-symlink .exe wholly inside this prefix's Program Files."""
    try:
        original = Path(path)
        if ".." in original.parts:
            return None
        lexical_root = Path(os.path.abspath(env.program_files))
        lexical_path = Path(os.path.abspath(original))
        relative = lexical_path.relative_to(lexical_root)

        # Do not let an inventory alias run a different executable under the plugin's name.
        # Refuse symlinks in the candidate path, including ancestor directories below Program Files.
        current = lexical_root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                return None

        prefix_root = env.drive_c.resolve(strict=True)
        program_files_root = env.program_files.resolve(strict=True)
        resolved = original.resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.casefold() != ".exe":
            return None
        resolved.relative_to(prefix_root)
        resolved.relative_to(program_files_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def resolve_standalone(
    plugin: PluginEntry, inventory: Inventory, env: Environment
) -> StandaloneResolution:
    """Find one exact-basename standalone executable for a plugin row.

    Matching is deliberately conservative: an absent or ambiguous match is not guessed. Symlinks
    and paths escaping the active prefix's Program Files are rejected. A matching executable may
    still be launched when its MSI ownership is unknown, but that is reported as unverified.
    """
    if plugin.kind not in {"vst2", "vst3", "aax", "standalone"}:
        return StandaloneResolution(None, "The selected row is not a plugin or standalone app.")

    target_stem = _stem(plugin.name)
    if not target_stem:
        return StandaloneResolution(None, "The selected plugin has no usable filename stem.")

    matches: dict[Path, PluginEntry] = {}
    for candidate in inventory.entries:
        if candidate.kind != "standalone" or candidate.disabled:
            continue
        # Most entries are unrelated host tools; reject by cheap metadata before stat/resolve work.
        if _stem(candidate.name) != target_stem:
            continue
        resolved = _inside_program_files(candidate.path, env)
        if resolved is None or _stem(resolved.name) != target_stem:
            continue
        matches[resolved] = candidate

    if not matches:
        return StandaloneResolution(
            None,
            f"No exact-name standalone executable was found under {env.program_files}; "
            "the separate Program Files (x86) tree is not searched.",
        )
    if len(matches) != 1:
        return StandaloneResolution(
            None,
            f"Multiple standalone executables match {plugin.name}; refusing to guess.",
        )

    executable, candidate = next(iter(matches.items()))
    verified = (
        plugin.msi is not None
        and candidate.msi == plugin.msi
        and plugin.integrity == "ok"
        and candidate.integrity == "ok"
    )
    reason = "" if verified else "MSI ownership/integrity is unverified for this executable."
    return StandaloneResolution(
        executable,
        reason,
        verified,
        candidate.expected_size if verified else None,
    )


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Wait for a detached child on a daemon thread so it is reaped without blocking the GUI."""
    try:
        process.wait()
    except OSError:
        # The GUI can still report the launch log; a wait error must not strand the parent window.
        pass


def launch_standalone(
    env: Environment, executable: Path, *, expected_size: int | None = None
) -> StandaloneLaunch:
    """Launch a prefix-local standalone app through this environment's custom Wine tree.

    The child is detached from the toolkit window, runs from its install directory, and receives
    the selected prefix and Wine server. Ambient loader overrides are removed so the selected
    custom tree is not silently replaced by the user's shell environment. Combined child output is
    written to a per-launch log under the selected HOME cache; process reaping is asynchronous.
    """
    resolved = _inside_program_files(Path(executable), env)
    if resolved is None:
        raise ValueError(f"not a prefix-local, non-symlink standalone executable: {executable}")
    wine = env.wine_binary
    if not wine.is_file():
        raise FileNotFoundError(f"custom Wine binary not found: {wine}")

    size_matches = expected_size is not None and resolved.stat().st_size == expected_size
    if expected_size is not None and not size_matches:
        raise ValueError(
            f"{resolved.name} changed size since the last inventory scan; refresh inventory before launch"
        )
    child_env = env.wine_env(include_profile_overrides=True)
    for variable in (
        "WINELOADER",
        "WINEDLLPATH",
        "WINEARCH",
        "WINEDLLOVERRIDES",
        "LD_PRELOAD",
        "WINELOADERNOEXEC",
        "WINEPRELOADRESERVE",
    ):
        child_env.pop(variable, None)
    # Do not let an inherited Wine trace setting produce an unbounded per-launch log. Preserve
    # LD_LIBRARY_PATH intentionally: custom Wine trees may need it to locate their private libraries.
    child_env["WINEDEBUG"] = "-all"

    cache_home = Path(os.environ.get("XDG_CACHE_HOME", ""))
    if not cache_home.is_absolute():
        cache_home = env.home / ".cache"
    log_dir = cache_home / "wpt" / "standalone"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", resolved.stem).strip("._")[:48] or "standalone"
    log_file = tempfile.NamedTemporaryFile(
        mode="w+b", prefix=f"{safe_stem}-", suffix=".log", dir=log_dir, delete=False
    )
    log_path = Path(log_file.name)
    try:
        process = subprocess.Popen(
            [str(wine), str(resolved)],
            cwd=str(resolved.parent),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
        )
    except BaseException:
        log_file.close()
        log_path.unlink(missing_ok=True)
        raise
    finally:
        if not log_file.closed:
            log_file.close()

    reaper = threading.Thread(
        target=_reap,
        args=(process,),
        name=f"wpt-standalone-reaper-{process.pid}",
        daemon=True,
    )
    reaper.start()
    return StandaloneLaunch(process=process, log_path=log_path, size_matches=size_matches)
