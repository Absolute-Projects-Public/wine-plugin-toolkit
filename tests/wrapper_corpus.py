"""Real-machine test: does the wrapper detector agree with reality?

The unit tests in `test_core.py` use synthetic files, which proves the markers work but
not that they fire on the installers people actually download. This walks real `.exe`
files on this machine, identifies each one, and, for anything claiming a Linux route. 
actually tries to unpack it and hands the first MSI to `msiinfo` to confirm it is a real
MSI rather than a file that merely ends in `.msi`.

Two independent opinions are printed for each file, because agreement is the point:

- ours (`wpt.wrappers.identify`)
- `7z`'s own container type for the same file, which knows nothing about our markers

    python3 tests/wrapper_corpus.py                     # find installers to test
    python3 tests/wrapper_corpus.py <file.exe> [...]    # test exactly these
    python3 tests/wrapper_corpus.py --max-size 400      # skip anything bigger (MB, default 400)
    python3 tests/wrapper_corpus.py --no-unpack         # identify only, run nothing
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt import wrappers as wrappers_mod  # noqa: E402

SKIP_NAMES = {"dxsetup.exe", "wine-mono-9.0.0-x86.msi", "wine-gecko-2.47.4-x86.msi"}


def seven_zip_type(path: Path) -> str:
    """What 7-Zip thinks this file is, an opinion formed without our markers."""
    if not shutil.which("7z"):
        return "7z absent"
    try:
        proc = subprocess.run(
            ["7z", "l", "-slt", str(path)], capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired):
        return "7z failed"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Type = "):
            return line.split("=", 1)[1].strip()
    return "unknown"


def msi_is_real(path: Path) -> tuple[bool, str]:
    """An MSI is only worth anything if msitools can read a product out of it."""
    if not shutil.which("msiinfo"):
        return (True, "msiinfo absent, not cross-checked")
    try:
        proc = subprocess.run(
            ["msiinfo", "export", str(path), "Property"],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (False, f"msiinfo failed: {exc}")
    if proc.returncode != 0:
        return (False, f"msiinfo exit {proc.returncode}")
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ProductName"):
            return (True, line.split("\t")[-1].strip() or "(no ProductName value)")
    return (True, "readable MSI, no ProductName row")


def gather(limit_mb: int, min_mb: float = 5.0) -> list[Path]:
    """Real installers, not every Wine stub, the prefix has hundreds of those."""
    found: list[Path] = []
    roots = [Path.home() / "Downloads", Path.home() / ".cache" / "winetricks", Path.home() / "Documents"]
    for directory in roots:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.exe")):
            if path.name.lower() in SKIP_NAMES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if min_mb * 1024 * 1024 <= size <= limit_mb * 1024 * 1024:
                found.append(path)
    # The prefix root is where a vendor wrapper gets left after being run, but its
    # Windows/ and Program Files/ trees are full of Wine's own stubs: root only.
    drive_c = Path.home() / ".wine-ableton" / "drive_c"
    if drive_c.is_dir():
        for path in sorted(drive_c.glob("*.exe")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if min_mb * 1024 * 1024 <= size <= limit_mb * 1024 * 1024:
                found.append(path)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", help="specific .exe files to test")
    parser.add_argument("--max-size", type=int, default=400, help="skip files bigger than this (MB)")
    parser.add_argument("--no-unpack", action="store_true", help="identify only")
    args = parser.parse_args(argv)

    targets = [Path(f).expanduser() for f in args.files] or gather(args.max_size)
    targets = [t for t in targets if t.is_file()]
    if not targets:
        print("no .exe files to test")
        return 0

    print(f"{len(targets)} wrapper(s) to identify; tools present: "
          f"{', '.join(wrappers_mod.unpacker_available()) or 'NONE'}")
    print()
    scratch = Path(tempfile.mkdtemp(prefix="wpt-corpus-"))
    problems: list[str] = []

    for path in targets:
        info = wrappers_mod.identify(path)
        other = seven_zip_type(path)
        print(f"{path.name}  ({info.size / 1_048_576:.1f} MB)")
        print(f"  ours      : {info.name}")
        print(f"  7z says   : {other}")

        if info.is_msi:
            print("  verdict   : already an MSI")
            continue

        if info.name == "unknown":
            problems.append(f"{path.name}: family not recognised (7z says {other})")
            print(f"  verdict   : NOT RECOGNISED - 7z says {other}; add a marker for this one")

        if info.needs_wine:
            print("  verdict   : Wine-only by design (no Linux unpacker reads this family)")
            print(f"  route     : wpt install \"{path.name}\" --run-wrapper")
            continue

        if args.no_unpack:
            print(f"  verdict   : {info.name}, Linux route available (not tried)")
            continue

        result = wrappers_mod.unpack_on_linux(path, scratch, info=info)
        for tool, note in result.attempts:
            print(f"    {tool:<22} {note}")
        if result.ok:
            real, note = msi_is_real(result.msi)
            status = "OK" if real else "SUSPECT"
            print(f"  verdict   : {status} - {result.route} -> {result.msi.name} ({note})")
            print(f"              {result.msi}")
            if not real:
                problems.append(f"{path.name}: produced an unreadable MSI ({note})")
        else:
            note = "no MSI inside" + (
                f" (payload folders: {', '.join(result.payload_dirs)})" if result.payload_dirs else ""
            )
            print(f"  verdict   : no MSI - {note}")
            tool_notes = " ".join(text for _, text in result.attempts).lower()
            if "unexpected setup data" in tool_notes:
                print("              the installed innoextract is older than this Inno version")
                continue
            # Not a failure on its own: plenty of installers legitimately carry no MSI.
            if info.name in ("WiX Burn bundle", "InstallShield", "Windows Installer wrapper"):
                problems.append(f"{path.name}: claimed {info.name} but yielded no MSI ({note})")
        print()

    shutil.rmtree(scratch, ignore_errors=True)

    if problems:
        print("needs attention:")
        for line in problems:
            print(f"  - {line}")
        return 1
    print("every wrapper was identified, and every claimed Linux route held up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
