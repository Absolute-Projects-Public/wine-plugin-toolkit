"""Vendor wrappers: getting from the `.exe` you download to the `.msi` this tool needs.

Neural DSP (and most vendors) ship an installer **.exe** that is a self-extracting front
end around an **.msi**. `msitools` only reads MSIs, so something has to bridge that gap,
and there are exactly two honest ways:

1. **unpack the wrapper on Linux** — the wrapper's own format decides which tool can do
   it: `innoextract` for Inno Setup, `7z` for NSIS / 7-Zip SFX / Burn bundles and for
   cabinets, `unshield` for InstallShield's own cabinet, `cabextract` for bare CABs and
   IExpress;
2. **run the wrapper under Wine once** — the vendor's own installer writes its MSI into
   the prefix (that is where the cached copies in `~/.wine-ableton` came from). This is
   what a user would do by hand; the toolkit just does it, waits, and picks the MSI up.

Advanced Installer's LZMA payload (what Neural DSP uses — the `Caphyon\\Advanced
Installer\\LZMA\\{GUID}` keys in the prefix registry are the fingerprint) is **not**
unpackable by any Linux tool, so for these wrappers route 2 is the one that works, and
saying so beats pretending otherwise — and beats spending minutes letting every tool fail
on a 500 MB file first.

Three things make route 1 usable in practice rather than theoretically:

- **the wrapper is identified before anything is run**, so the chain is per family
  instead of "try everything and hope", and a family nothing can read is refused outright;
- **containers are found by content, not by file name.** A WiX Burn bundle hands its
  payload over as members called `a0`…`a13` and a DirectX redist as cabinets with the
  right name and no MSI inside, so the search looks for the cabinet magic (`MSCF`) and
  the MSI magic (OLE compound document, `d0cf11e0a1b11ae1`) rather than for `*.cab`
  and `*.msi`. Verified on `vc_redist.x64.exe`: `cabextract` yields `a9`/`a10`, which
  `msiinfo` reads as *Microsoft Visual C++ 2022 X64 Minimum/Additional Runtime*;
- **nesting is followed** — a member that is itself a cabinet (the `Windows6.1-KB2999226`
  cabs inside that same bundle) is opened in turn.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import msi as msi_mod
from .environment import Environment

# How much of a wrapper to search for fingerprints. Installers put their identity in the
# PE resources near the front; self-extractors sometimes only name themselves in an
# overlay at the very end, so the tail is read too (but only a slice — these files reach
# 500 MB and reading them whole is not worth the seconds).
HEAD_BYTES = 6 * 1024 * 1024
TAIL_BYTES = 1024 * 1024

# How deep to follow container-inside-container. Two levels covers Burn and InstallShield
# without letting a pathological file spin.
MAX_NESTING = 2

# Enough of a member to judge what it is without reading a whole 500 MB payload.
SNIFF_BYTES = 256 * 1024

CAB_MAGIC = b"MSCF"
MSI_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"          # OLE compound document
MSI_MARKERS = (b"Windows Installer", b"Microsoft Installer")


@dataclass(frozen=True)
class Family:
    """One installer family: how to spot it, and what can open it."""

    name: str
    markers: tuple[bytes, ...] = ()
    tools: tuple[str, ...] = ()
    needs_wine: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return self.name


# Order matters: the first family whose markers hit wins, so the one that must not be
# guessed at (Advanced Installer) and the unambiguous signatures (NSIS) lead.
FAMILIES: tuple[Family, ...] = (
    Family(
        name="Advanced Installer (LZMA)",
        markers=(b"Caphyon", b"Advanced Installer"),
        needs_wine=True,
        note=(
            "Advanced Installer LZMA payload: no Linux tool can unpack it (7z lists the PE "
            "sections and hands back a blob it cannot open)"
        ),
    ),
    Family(
        name="NSIS",
        markers=(b"Nullsoft", b"NSIS Error", b"NSIS Installer"),
        tools=("7z", "7za", "7zr", "cabextract"),
    ),
    Family(
        name="Inno Setup",
        markers=(b"Inno Setup Setup Data", b"InnoSetupVersion", b"JR.Inno.Setup", b"rDlPtS02"),
        tools=("innoextract", "7z", "7za"),
    ),
    Family(
        name="InstallShield",
        markers=(
            b"InstallShield",
            b"Install Shield",
            b"ISSetupStream",
            b"setup.inx",
            b"_setup.dll",
            b"data1.cab",
        ),
        tools=("unshield", "7z", "7za", "cabextract"),
    ),
    Family(
        name="WiX Burn bundle",
        markers=(b"WixBundle", b"wixburn", b"Burn engine"),
        tools=("7z", "7za", "cabextract"),
        note="msiexec-based, with its payload appended as a cabinet",
    ),
    Family(
        name="7-Zip self-extractor",
        markers=(b"7z\xbc\xaf\x27\x1c",),
        tools=("7z", "7za", "7zr"),
    ),
    Family(
        name="IExpress / CAB self-extractor",
        markers=(b"IExpress", b"MSCF"),
        tools=("cabextract", "7z", "7za"),
    ),
    Family(
        name="Windows Installer wrapper",
        markers=(b"msiexec", b"Windows Installer"),
        tools=("7z", "7za", "cabextract"),
        note="calls msiexec on an MSI it carries",
    ),
)

NEEDS_WINE = FAMILIES[0]

# Linux-side unpackers, tried in order when no family is known. Each is only used if it
# is installed.
UNPACKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("7z", ("x", "-y")),
    ("7za", ("x", "-y")),
    ("7zr", ("x", "-y")),
    ("cabextract", ("-q", "-d")),
    ("innoextract", ("-e", "-d")),
)

# Fingerprints that say "this one needs Wine" rather than "try harder".
ADVANCED_INSTALLER_MARKER = b"Caphyon"

# Payload directories a wrapper occasionally hands over raw instead of as an MSI.
PAYLOAD_DIRS = ("VST3DIR", "VSTDIR", "AAXDIR", "APPDIR", "PREDIR")

# 7z/7za/7zr are the same extractor under three names (Arch ships all three), so a chain
# must pick one rather than unpack the same file three times.
SEVEN_ZIP_GROUP = ("7z", "7za", "7zr")


@dataclass
class WrapperInfo:
    """What a file is, before anything is run."""

    path: Path
    size: int = 0
    is_msi: bool = False
    family: Family | None = None

    @property
    def name(self) -> str:
        return self.family.name if self.family else "unknown"

    @property
    def needs_wine(self) -> bool:
        return bool(self.family and self.family.needs_wine)

    @property
    def tools(self) -> tuple[str, ...]:
        """The unpackers worth trying: the family's own, or every general one."""
        if self.family and self.family.tools:
            return self.family.tools
        return tuple(name for name, _ in UNPACKERS)


@dataclass
class WrapperResult:
    msi: Path | None = None
    route: str = ""                 # "already-msi" | "cached" | "unpacked:<tool>" | "wine" | "none"
    detail: str = ""
    attempts: list[tuple[str, str]] = field(default_factory=list)
    family: str = ""
    payload_dirs: list[str] = field(default_factory=list)
    also_found: list[Path] = field(default_factory=list)   # other MSIs in the same wrapper

    @property
    def ok(self) -> bool:
        return self.msi is not None


@dataclass
class Harvest:
    """What a directory turned out to hold."""

    msis: list[Path] = field(default_factory=list)
    attempts: list[tuple[str, str]] = field(default_factory=list)
    payload_dirs: list[str] = field(default_factory=list)
    nested_cabs: int = 0


def unpacker_available() -> list[str]:
    return [name for name, _ in UNPACKERS if shutil.which(name)]


def resolve_tools(tools: tuple[str, ...] | list[str]) -> list[str]:
    """One entry per distinct tool, the installed spelling preferred."""
    resolved: list[str] = []
    seven_zip_done = False
    for tool in tools:
        if tool in SEVEN_ZIP_GROUP:
            if seven_zip_done:
                continue
            picked = next((name for name in SEVEN_ZIP_GROUP if shutil.which(name)), None)
            resolved.append(picked or tool)
            seven_zip_done = True
        elif tool not in resolved:
            resolved.append(tool)
    return resolved


def _read_slices(path: Path) -> bytes:
    """The head and tail of a file, which is all the fingerprints ever live in."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
            if size > HEAD_BYTES + TAIL_BYTES:
                handle.seek(-TAIL_BYTES, 2)
                return head + handle.read(TAIL_BYTES)
            return head
    except OSError:
        return b""


def identify(path: Path) -> WrapperInfo:
    """Work out which installer family a download belongs to.

    Cheap and read-only: the file is never executed and nothing is unpacked.
    """
    path = Path(path)
    info = WrapperInfo(path=path)
    try:
        info.size = path.stat().st_size
    except OSError:
        pass
    if path.suffix.lower() == ".msi":
        info.is_msi = True
        return info
    if path.suffix.lower() != ".exe":
        return info

    blob = _read_slices(path)
    if not blob:
        return info

    # NSIS installers start with the first-header magic \xEF\xBE\xAD\xDE. Worth checking by
    # offset: a stray 0xDEADBEEF deeper in the file means nothing.
    if blob[:4] == b"\xef\xbe\xad\xde":
        info.family = next(f for f in FAMILIES if f.name == "NSIS")
        return info

    for family in FAMILIES:
        if any(marker in blob for marker in family.markers):
            info.family = family
            return info
    return info


def _looks_advanced_installer(path: Path) -> bool:
    """Advanced Installer wrappers embed an LZMA payload no Linux tool can read."""
    return ADVANCED_INSTALLER_MARKER in _read_slices(Path(path))


def msi_product_name(path: Path, timeout: int = 60) -> str:
    """The MSI's own ProductName, via msitools. Empty when it is not an MSI (or no msitools)."""
    if not shutil.which("msiinfo"):
        return ""
    try:
        proc = subprocess.run(
            ["msiinfo", "export", str(path), "Property"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ProductName"):
            return line.split("\t")[-1].strip().strip("\r")
    return ""


def is_msi_file(path: Path) -> bool:
    """An MSI by content: an OLE compound document that msitools agrees is an installer.

    File names cannot be trusted here — a Burn bundle's MSIs come out of its cabinet as
    `a9`, `a10` — and neither can the SummaryInformation string: a real Neural DSP MSI was
    checked that carries no "Windows Installer"/"Microsoft Installer" text in its first
    256 KB at all. So: OLE magic first, then let msitools be the judge when it is present.
    """
    try:
        if path.stat().st_size < 4096:
            return False
        with path.open("rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError:
        return False
    if not head.startswith(MSI_MAGIC):
        return False
    if any(marker in head for marker in MSI_MARKERS):
        return True
    if shutil.which("msiinfo"):
        return bool(msi_product_name(path))
    # best effort without msitools: an OLE file of that shape inside an installer payload
    return True


def is_cab_file(path: Path) -> bool:
    """A cabinet by content, whatever it is called."""
    try:
        with path.open("rb") as handle:
            return handle.read(4) == CAB_MAGIC
    except OSError:
        return False


def _first_line(text: str, limit: int = 90) -> str:
    """One short, useful line out of a tool's chatter, for the report."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "locale=" in line or line.startswith(("7-Zip ", "Copyright (c)", "Scanning the drive")):
            continue
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        return line
    return ""


def _command(tool: str, source: Path, target: Path) -> list[str]:
    if tool in SEVEN_ZIP_GROUP:
        return [tool, "x", "-y", f"-o{target}", str(source)]
    if tool == "cabextract":
        return [tool, "-q", "-d", str(target), str(source)]
    if tool == "innoextract":
        return [tool, "-e", "-d", str(target), str(source)]
    if tool == "unshield":
        return [tool, "x", "-d", str(target), str(source)]
    return [tool, str(source)]


def _run_tool(tool: str, source: Path, target: Path, timeout: int = 1800) -> tuple[int, str]:
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            _command(tool, source, target), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 1, str(exc)
    detail = _first_line(proc.stderr or "") or _first_line(proc.stdout or "")
    return proc.returncode, detail


def _harvest(directory: Path, depth: int = 0) -> Harvest:
    """Look inside what a tool produced, by content rather than by file name.

    MSIs and cabinets are recognised by their magic bytes, so a Burn bundle's `a9` and an
    XPSP self-extractor's `[0]` are found just as readily as a file that is actually
    called `something.msi`. Cabinets are opened in turn.
    """
    result = Harvest()
    for entry in sorted(directory.rglob("*")):
        if entry.is_file() and is_msi_file(entry):
            result.msis.append(entry)

    result.payload_dirs = sorted(
        {part.name for part in directory.rglob("*") if part.is_dir() and part.name in PAYLOAD_DIRS}
    )
    if result.msis or depth >= MAX_NESTING:
        return result

    containers = [
        entry
        for entry in sorted(directory.rglob("*"))
        if entry.is_file() and entry.stat().st_size > 1024 and is_cab_file(entry)
    ]
    for container in containers[:12]:
        if container.name.lower().endswith(".msi"):
            continue
        for tool in resolve_tools(("cabextract", "7z")):
            if not shutil.which(tool):
                continue
            target = container.parent / f"inner-{container.name}-{tool}"
            _run_tool(tool, container, target)
            result.nested_cabs += 1
            inner = _harvest(target, depth + 1)
            result.msis.extend(m for m in inner.msis if m not in result.msis)
            result.payload_dirs = sorted(set(result.payload_dirs) | set(inner.payload_dirs))
            result.attempts.extend(inner.attempts)
            if result.msis:
                result.attempts.append((f"{tool} on {container.name}", f"ok: {result.msis[0].name}"))
                return result
    if result.nested_cabs:
        result.attempts.append(
            ("containers", f"{result.nested_cabs} opened, no MSI inside")
        )
    return result


def wrapper_workdir(scratch: Path) -> Path:
    """Where wrapper unpacking is allowed to happen.

    Deliberately a **sibling** of the MSI scratch dir, not a child of it: `msi.extract()`
    empties its scratch before every extraction (so one product's payload cannot leak into
    the next plan), and that wipe would delete the MSI we just unpacked out of the
    wrapper. Found the hard way — the CLI picked the MSI up and then could not open it.
    """
    scratch = Path(scratch)
    return scratch.parent / f"{scratch.name}-unpack"


def _materialise(msi: Path, workdir: Path) -> Path:
    """Give a nameless member a `.msi` name so the rest of the tool reads clearly.

    Always overwrites: a leftover copy from a previous wrapper in the same workdir would
    otherwise be handed back as if it came from this one (that is how the x86 dotnet
    wrapper ended up reporting an x64 MSI).
    """
    if msi.suffix.lower() == ".msi":
        return msi
    named = workdir / f"{msi.parent.name}-{msi.name}.msi"
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(msi, named)
    except OSError:
        return msi
    return named


def _hint_words(hint: str) -> list[str]:
    """Tokens worth matching on: 'vc_redist.x64' -> ['redist', 'x64'].

    Three characters, not four: `x64`, `x86` and `arm` carry the architecture, and getting
    that wrong picks the Arm64 runtime for an x64 machine.
    """
    return [w.lower() for w in hint.replace("-", " ").replace("_", " ").replace(".", " ").split() if len(w) >= 3]


ARCH_TOKENS = ("x64", "x86", "arm64", "arm")


def _path_arch(path: Path) -> int:
    """Architecture hinted by the member's own path: 2 x64, 1 x86, 0 unknown, -1 arm.

    Self-extractors that ship every architecture put them in sibling folders named
    `x64/`, `x86/`, `arm64/`, with identical file names inside. The Wine prefix is x86_64,
    so an unqualified choice must not land on the Arm64 build.
    """
    parts = [path.name.lower(), *(parent.name.lower() for parent in list(path.parents)[:2])]
    text = " ".join(parts)
    if "arm" in text:
        return -1
    if "x64" in text or "amd64" in text:
        return 2
    if "x86" in text or "win32" in text:
        return 1
    return 0


def _pick_msi(candidates: list[Path], hint: str) -> tuple[Path, list[Path]]:
    """Choose between MSIs found in one wrapper, and report the others.

    Candidates are scored on the MSI's *own* ProductName where msitools can read it — a
    Burn bundle's members are called `a0`…`a13`, so the file name says nothing, while the
    ProductName says "Microsoft Visual C++ 2022 X64 Minimum Runtime".

    Architecture outweighs everything else, and deliberately so: a wrapper named
    `…-win-x86` whose payload holds both architectures must not resolve to the x64 MSI
    just because that one is bigger.
    """
    if len(candidates) == 1:
        return candidates[0], []
    words = _hint_words(hint)
    wanted_arch = next((token for token in ARCH_TOKENS if token in words), "")

    def score(path: Path) -> tuple[int, int, int, int]:
        name = (msi_product_name(path) or path.stem).lower()
        arch_ok = 1 if (not wanted_arch or wanted_arch in name) else 0
        return (arch_ok, sum(1 for word in words if word in name), _path_arch(path), path.stat().st_size)

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[0], ranked[1:]


def unpack_on_linux(
    wrapper: Path,
    scratch: Path,
    info: WrapperInfo | None = None,
    product_hint: str = "",
    timeout: int = 1800,
) -> WrapperResult:
    """Try the unpackers that suit this wrapper and look for an MSI in the result."""
    wrapper = Path(wrapper)
    scratch = Path(scratch)
    info = info or identify(wrapper)
    result = WrapperResult(family=info.name)
    workdir = wrapper_workdir(scratch)

    if info.needs_wine:
        # Nothing on Linux can read this one, so do not spend minutes proving it.
        result.attempts.append(("(skipped)", info.family.note if info.family else ""))
        return result

    for tool in resolve_tools(info.tools):
        if not shutil.which(tool):
            result.attempts.append((tool, "not installed"))
            continue
        target = workdir / f"unpack-{tool}"
        code, note = _run_tool(tool, wrapper, target, timeout=timeout)
        harvest = _harvest(target)
        result.attempts.extend(harvest.attempts)
        if harvest.payload_dirs:
            result.payload_dirs = sorted(set(result.payload_dirs) | set(harvest.payload_dirs))
        if harvest.msis:
            chosen, others = _pick_msi(harvest.msis, product_hint or wrapper.stem)
            chosen = _materialise(chosen, workdir)
            result.msi = chosen
            result.route = f"unpacked:{tool}"
            result.also_found = others
            result.detail = f"{info.name} wrapper: {tool} produced {chosen.name}"
            if others:
                result.detail += (
                    f" (+{len(others)} more MSI(s) in the same wrapper: "
                    + ", ".join(m.name for m in others[:4])
                    + ")"
                )
            result.attempts.append((tool, f"ok: {chosen.name}"))
            return result
        result.attempts.append((tool, note or f"exit {code}, no MSI inside"))
    return result


def run_wrapper(env: Environment, wrapper: Path, timeout: int = 2400) -> tuple[int, str]:
    """Run the vendor's own installer under Wine, inside this prefix.

    This is the user's by-hand step, automated: the wrapper extracts its MSI into the
    prefix and installs from there. It can take minutes and may put up a window, so the
    caller is expected to tell the user what is happening.
    """
    command = [str(env.wine_binary), str(wrapper)]
    try:
        proc = subprocess.run(
            command, env=env.wine_env(), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, f"the vendor installer did not finish within {timeout // 60} minutes"
    except OSError as exc:
        return 1, str(exc)
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode, detail[-2000:]


def cached_msis(env: Environment) -> set[Path]:
    # a wrapper's MSI can be sitting in Wine's msiexec cache rather than a vendor folder
    return set(msi_mod.find_extracted_msis(env.prefix, include_installer_cache=True))


def prepare_msi(
    env: Environment,
    path: Path,
    scratch: Path,
    product_hint: str | None = None,
    allow_wine: bool = False,
    timeout: int = 2400,
    log=None,
) -> WrapperResult:
    """Turn whatever the user has (`.msi` or vendor `.exe`) into a usable MSI.

    Order of preference, cheapest and safest first:

    1. it is already an MSI;
    2. the wrapper has been run before, so its MSI is cached in the prefix;
    3. a Linux unpacker suited to its family can pull an MSI out of it;
    4. run the wrapper under Wine (only if `allow_wine`) and pick up what it caches.
    """
    path = Path(path)
    say = log or (lambda _message: None)
    hint = product_hint or path.stem

    info = identify(path)

    if info.is_msi:
        return WrapperResult(
            msi=path, route="already-msi", detail=f"{path.name} is an MSI already", family=info.name
        )

    if path.suffix.lower() != ".exe":
        return WrapperResult(
            route="none", detail=f"{path.name}: not an MSI or a .exe wrapper", family=info.name
        )

    if info.family:
        say(f"{path.name}: {info.name} wrapper"
            + (f" - {info.family.note}" if info.family.note else ""))
    else:
        say(f"{path.name}: family not recognised, will try every installed unpacker")

    before = cached_msis(env)
    existing = _match_cached(before, hint)
    if existing:
        return WrapperResult(
            msi=existing,
            route="cached",
            family=info.name,
            detail=(
                f"{path.name} was run before, so its MSI is already in the prefix "
                f"({existing.name}) - no need to run it again"
            ),
        )

    unpacked = WrapperResult(family=info.name)
    if not info.needs_wine:
        say(f"unpacking {path.name} on Linux (no Wine yet) ...")
        unpacked = unpack_on_linux(path, scratch, info=info, product_hint=hint, timeout=timeout)
        if unpacked.ok:
            return unpacked
        for tool, note in unpacked.attempts:
            say(f"  {tool}: {note}")

    if not allow_wine:
        reason = (
            info.family.note
            if info.needs_wine and info.family and info.family.note
            else "no installed unpacker could read it"
        )
        detail = (
            f"{reason}. Two options: run it under Wine once with "
            f"`wpt install {path.name} --run-wrapper` (the vendor installer writes its MSI "
            f"into the prefix, which is where this tool picks it up), or download the "
            f"bare .msi if the vendor offers one."
        )
        if unpacked.payload_dirs:
            detail += (
                " Note: the unpack did produce payload folders ("
                + ", ".join(unpacked.payload_dirs)
                + ") - an MSI is what this tool installs from, so those files were left alone."
            )
        return WrapperResult(
            route="none",
            detail=detail,
            attempts=unpacked.attempts,
            family=info.name,
            payload_dirs=unpacked.payload_dirs,
        )

    say(f"running {path.name} under Wine - the vendor installer will extract its MSI into the prefix")
    code, detail = run_wrapper(env, path, timeout=timeout)
    say(f"  vendor installer exited {code}")
    after = cached_msis(env)
    new = _match_cached(after - before, hint) or _match_cached(after, hint)
    if new:
        return WrapperResult(
            msi=new, route="wine", family=info.name,
            detail=f"the wrapper cached {new.name} in the prefix",
        )
    return WrapperResult(
        route="none",
        family=info.name,
        detail=(
            f"the wrapper ran (exit {code}) but left no MSI in the prefix"
            + (f": {detail.splitlines()[-1]}" if detail else "")
        ),
        attempts=unpacked.attempts,
    )


def _match_cached(candidates: set[Path], hint: str) -> Path | None:
    """Pick the cached MSI whose product name best matches the wrapper's file name."""
    if not candidates:
        return None
    words = [w.lower() for w in hint.replace("-", " ").replace("_", " ").split() if len(w) >= 4]
    scored: list[tuple[int, float, Path]] = []
    for path in candidates:
        name = path.stem.lower()
        score = sum(1 for word in words if word in name)
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        scored.append((score, stamp, path))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0]
    if best[0] == 0 and len(scored) > 1:
        # nothing matched by name: only accept a single unambiguous candidate
        return None
    return best[2]


def render_info(info: WrapperInfo) -> str:
    """A one-screen answer to "what is this file, and can you use it?"."""
    lines = [f"{info.path.name}", f"  size        : {info.size / 1_048_576:.1f} MB"]
    if info.is_msi:
        lines.append("  kind        : MSI - no wrapper to bridge, install it directly")
        return "\n".join(lines)
    lines.append(f"  wrapper     : {info.name}")
    if info.family and info.family.note:
        lines.append(f"  note        : {info.family.note}")
    if info.needs_wine:
        lines.append("  linux route : none - this one has to run under Wine to yield its MSI")
        lines.append("  command     : wpt install <file> --run-wrapper")
    else:
        route_tools = resolve_tools(info.tools)
        installed = [t for t in route_tools if shutil.which(t)]
        missing = [t for t in route_tools if not shutil.which(t)]
        lines.append(f"  linux route : {' > '.join(installed) if installed else 'no suitable tool installed'}")
        if missing:
            lines.append(f"  not present : {', '.join(missing)}")
        if not installed:
            lines.append("  command     : install one of the tools above, or use --run-wrapper")
    return "\n".join(lines)
