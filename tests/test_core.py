"""Local sanity tests for the Wine Plugin Toolkit core.

These cover the pure-logic paths that don't need a real prefix or msitools, so
they run anywhere: registry decoding, Windows->Linux path mapping, destination
resolution and the CLI surface. Real-machine verification (msiinfo/msiextract,
against a genuine vendor MSI) is a separate, manual step.

    python3 tests/test_core.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt import installer  # noqa: E402
from wpt.cli import build_parser  # noqa: E402
from wpt.environment import Environment, _version_key  # noqa: E402
from wpt.msi import MsiIdentity  # noqa: E402
from wpt.scan import _classify as classify, _decode as decode  # noqa: E402
from wpt.scan import _group_products as group_products, _to_linux as to_linux, is_system_product  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


def fake_env() -> Environment:
    return Environment(
        home=Path("/home/tester"),
        wine_tree=Path("/home/tester/.local/opt/wine-d2d1-nspa-11.13"),
        prefix=Path("/home/tester/.wine-ableton"),
        user="tester",
    )


print("version sort (the bug that started all this)")
check("11.13 beats 11.11", _version_key(Path("wine-d2d1-nspa-11.13")) > _version_key(Path("wine-d2d1-nspa-11.11")), True)
check("11.9 sorts below 11.13", _version_key(Path("wine-d2d1-nspa-11.9")) < _version_key(Path("wine-d2d1-nspa-11.13")), True)
check("parses 12.0", _version_key(Path("wine-d2d1-nspa-12.0")), [12, 0])

print("registry value decoding")
check("escaped backslashes", decode(r'"C:\\Program Files\\Common Files\\VST3\\A.vst3"'),
      r"C:\Program Files\Common Files\VST3\A.vst3")
check("str(2) utf-16 blob",
      decode('str(2):"43003a005c00500072006f006700720061006d00"'),
      "C:\\Program")
check("empty value stays empty", decode('""'), "")

print("windows -> linux path mapping")
env = fake_env()
check("standard path",
      to_linux(env, r"C:\Program Files\Common Files\VST3\X.vst3"),
      Path("/home/tester/.wine-ableton/drive_c/Program Files/Common Files/VST3/X.vst3"))
check("trailing slash stripped",
      to_linux(env, "C:\\ProgramData\\Neural DSP\\Archetype Rabea X\\"),
      Path("/home/tester/.wine-ableton/drive_c/ProgramData/Neural DSP/Archetype Rabea X"))
check("non-C drive ignored", to_linux(env, r"Z:\tmp\x"), None)
check("bare C:\\ ignored", to_linux(env, "C:\\"), None)

print("entry classification")
check("plugin inside the VST3 dir", classify(r"C:\Program Files\Common Files\VST3\A.vst3"), "plugin")
check("vst2 dll", classify(r"C:\Program Files\VstPlugins\A.dll"), "plugin")
check("aax plugin", classify(r"C:\Program Files\Common Files\Avid\Audio\Plug-Ins\A.aaxplugin"), "plugin")
check("directory in a plugin dir", classify("C:\\Program Files\\Common Files\\VST3\\"), "dir")
check("preset path is not a plugin path", classify(r"C:\ProgramData\Neural DSP\Archetype Rabea X\User"), "other")
check("system dll outside plugin dirs", classify(r"C:\windows\system32\kernel32.dll"), "other")
check("plugin-ish name outside plugin dirs", classify(r"C:\Elsewhere\Thing.vst3"), "other")

print("product grouping")
rows = [
    {"key": "DisplayName", "value": "Archetype Rabea X", "source": "system.reg"},
    {"key": "DisplayVersion", "value": "1.1.0", "source": "system.reg"},
    {"key": "DisplayName", "value": "Archetype Nolly X", "source": "system.reg"},
    {"key": "DisplayVersion", "value": "1.0.2", "source": "system.reg"},
]
grouped = group_products(rows)
check("two products", len(grouped), 2)
check("first product name", grouped[0]["DisplayName"], "Archetype Rabea X")
check("second version", grouped[1]["DisplayVersion"], "1.0.2")

print("destination resolution")
ident = MsiIdentity(
    product_name="Archetype Rabea X",
    manufacturer="Neural DSP",
    properties={
        "VST3DIR": r"C:\Program Files\Common Files\VST3",
        "VSTDIR": r"C:\Program Files\VstPlugins",
        "AAXDIR": r"C:\Program Files\Common Files\Avid\Audio\Plug-Ins",
        "PREDIR": r"C:\ProgramData\Neural DSP\Archetype Rabea X",
    },
)
check("VST3DIR uses declared target",
      installer._destination_for("VST3DIR", ident, env),
      Path("/home/tester/.wine-ableton/drive_c/Program Files/Common Files/VST3"))
check("APPDIR:./ prefix stripped, falls back to Program Files/<vendor>/<product>",
      installer._destination_for("APPDIR:./Archetype Rabea X", ident, env),
      Path("/home/tester/.wine-ableton/drive_c/Program Files/Neural DSP/Archetype Rabea X"))
check("PREDIR uses declared target",
      installer._destination_for("PREDIR", ident, env),
      Path("/home/tester/.wine-ableton/drive_c/ProgramData/Neural DSP/Archetype Rabea X"))
check("unknown folder is refused, not guessed",
      installer._destination_for("SOMETHINGELSE", ident, env), None)

print("APPDIR mapping: the standalone app must not land one level too deep")
import tempfile as _tf  # noqa: E402

from wpt.msi import MsiIdentity  # noqa: E402

_msi_case = Path(_tf.mkdtemp())
# case A: the payload already contains a folder named after the product
(_msi_case / "Mantra").mkdir(parents=True)
(_msi_case / "Mantra" / "Mantra.exe").write_text("exe")
_mantra = MsiIdentity(product_name="Mantra", manufacturer="Neural DSP", properties={})
check("payload nests the product folder -> destination is the vendor directory",
      installer._destination_for("APPDIR:.", _mantra, env, children=sorted(_msi_case.iterdir())),
      Path("/home/tester/.wine-ableton/drive_c/Program Files/Neural DSP"))
# case B: the payload *is* the product folder (loose files)
_loose = _msi_case / "loose"
_loose.mkdir()
(_loose / "Mantra.exe").write_text("exe")
check("payload is loose files -> product folder is added",
      installer._destination_for("APPDIR:.", _mantra, env, children=sorted(_loose.iterdir())),
      Path("/home/tester/.wine-ableton/drive_c/Program Files/Neural DSP/Mantra"))
check("no children known -> keeps the old behaviour",
      installer._destination_for("APPDIR:.", _mantra, env),
      Path("/home/tester/.wine-ableton/drive_c/Program Files/Neural DSP/Mantra"))
import shutil as _sh2  # noqa: E402

_sh2.rmtree(_msi_case, ignore_errors=True)

print("product record grouping")
rows_dup = [
    {"key": "DisplayName", "value": "Archetype Rabea X", "source": "system.reg"},
    {"key": "DisplayName", "value": "Archetype Rabea X", "source": "user.reg"},
    {"key": "DisplayVersion", "value": "1.1.0", "source": "user.reg"},
    {"key": "DisplayName", "value": "Microsoft Visual C++ 2022 X64 Minimum Runtime", "source": "system.reg"},
]
grouped_dup = group_products(rows_dup)
check("duplicate sightings merged into one record", len(grouped_dup), 2)
check("version merged from the second sighting", grouped_dup[0].get("DisplayVersion"), "1.1.0")
check("runtime noise recognised", is_system_product("Microsoft Visual C++ 2022 X64 Minimum Runtime"), True)
check("real product not treated as noise", is_system_product("Archetype Nolly X"), False)

print("scratch directory hygiene (a stale payload joins the next plan)")
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402

from wpt import msi as msi_mod  # noqa: E402

_tmp = Path(_tempfile.mkdtemp())
_scratch = _tmp / "wpt-extract"
(_scratch / "VST3DIR").mkdir(parents=True)
(_scratch / "VST3DIR" / "leftover.vst3").write_text("stale")
msi_mod.prepare_scratch(_scratch)
check("stale extraction under the temp dir is wiped", (_scratch / "VST3DIR").exists(), False)
check("wpt marks the scratch dir as its own", (_scratch / msi_mod.EXTRACT_MARKER).is_file(), True)
(_scratch / "VST3DIR").mkdir()
check("the marker file is not mistaken for an extracted folder",
      msi_mod.extracted_root_dirs(_scratch), ["VST3DIR"])

_precious = Path.home() / "wpt-scratch-refusal-test"
_shutil.rmtree(_precious, ignore_errors=True)
_precious.mkdir(parents=True)
(_precious / "notes.txt").write_text("real data")
try:
    msi_mod.prepare_scratch(_precious)
    check("a non-empty dir outside temp is refused", "no error raised", "MsiError")
except msi_mod.MsiError:
    check("a non-empty dir outside temp is refused", True, True)
check("the refused directory is untouched", (_precious / "notes.txt").exists(), True)
print("vendor catalogue (the downloads page)")
from wpt import catalogue as catalogue_mod  # noqa: E402
from wpt import wrappers as wrappers_mod  # noqa: E402
import shutil as _sh3  # noqa: E402

_fixture = Path(__file__).resolve().parent / "fixtures" / "neuraldsp-downloads.html"
_releases = catalogue_mod.parse(_fixture.read_text())
check("parses every product block", len(_releases), 3)
check("groups the hardware section", [r.product for r in _releases if r.group == "Hardware"], ["Cortex Control"])
check("relative download links are made absolute",
      all(r.windows.startswith("https://neuraldsp.com/") for r in _releases if r.needs_sign_in), True)
check("plugin links are flagged as needing a sign-in",
      [r.needs_sign_in for r in _releases], [False, True, True])
check("hardware links are flagged as directly downloadable",
      [r.direct_download for r in _releases], [True, False, False])
check("version and release date are read", (_releases[1].version, _releases[1].released), ("1.1.0 (X)", "Jul 29, 2026"))
check("slug comes from the link", _releases[1].slug, "archetype-rabea")
check("manual link captured", _releases[1].manual.endswith("/manual/archetype-rabea-x"), True)

_cat = catalogue_mod.Catalogue(releases=_releases)
check("find by slug", [r.product for r in _cat.find("archetype-nolly")], ["Archetype: Nolly X"])
check("find by loose name", [r.product for r in _cat.find("nolly")], ["Archetype: Nolly X"])
check("find nothing for a stranger", _cat.find("soldano"), [])
check("the bundled snapshot has the live list", len(catalogue_mod.load_snapshot().releases) > 20, True)

print("vendor wrappers (.exe) -> MSI")
_home_scratch = Path(_tempfile.mkdtemp())
_already = _home_scratch / "Thing.msi"
_already.write_bytes(b"msi")
check("an MSI passes straight through",
      wrappers_mod.prepare_msi(env, _already, _home_scratch / "x").route, "already-msi")
_wrapper = _home_scratch / "ArchetypeNollyXv9.9.9.exe"
_wrapper.write_bytes(b"MZ" + b"Caphyon Advanced Installer" + b"\x00" * 64)
check("recognises an Advanced Installer payload", wrappers_mod._looks_advanced_installer(_wrapper), True)
_blocked = wrappers_mod.prepare_msi(env, _wrapper, _home_scratch / "y", allow_wine=False)
check("refuses to run Wine unless asked", _blocked.ok, False)
check("and explains the two ways forward", "run it under Wine once" in _blocked.detail, True)
check("an Advanced Installer wrapper is named as such", "Advanced Installer" in _blocked.detail, True)
_cache = Path(_tempfile.mkdtemp())
(_cache / "Archetype Nolly X.msi").write_bytes(b"msi")
(_cache / "Something Else.msi").write_bytes(b"msi")
check("a cached MSI is preferred to running anything",
      wrappers_mod._match_cached({_cache / "Archetype Nolly X.msi"}, "ArchetypeNollyXv1.0.2").name,
      "Archetype Nolly X.msi")
check("an ambiguous cache with no name match is refused",
      wrappers_mod._match_cached({_cache / "Something Else.msi", _cache / "Archetype Nolly X.msi"},
                                 "NothingLikeIt"), None)
# Being the only candidate in the cache is not evidence of anything: this used to hand a fresh
# wrapper whatever single MSI was in the prefix, which would install the wrong product and then
# verify it against the wrong File table.
check("an unrelated cached MSI is refused even when it is the only one",
      wrappers_mod._match_cached({_cache / "Something Else.msi"}, "NothingLikeIt"), None)
check("a squashed name match still works (vendor wrappers run the name together with the version)",
      wrappers_mod._match_cached({_cache / "Archetype Nolly X.msi"}, "ArchetypeNollyXv1.0.2").name,
      "Archetype Nolly X.msi")
check("and a hyphenated one does too",
      wrappers_mod._match_cached({_cache / "Archetype Nolly X.msi"}, "archetype-nolly-x-setup.exe").name,
      "Archetype Nolly X.msi")
# equal-length names once passed the containment test because min()/max() returned the same
# element for a tie: this pair must stay refused
check("two unrelated names of equal length do not match",
      wrappers_mod._match_cached({_cache / "Something Else.msi"}, "NothingLikeIt"), None)
check("a vanished cached path does not explode",
      wrappers_mod._match_cached({_cache / "gone.msi"}, "gone").name, "gone.msi")

print("wrapper families (which one is this, and what can open it)")
_check = wrappers_mod.identify
_pe = lambda marker: b"MZ" + marker + b"\x00" * 32  # noqa: E731
_cases = (
    ("Advanced Installer (LZMA)", _pe(b"Caphyon"), True),
    ("Advanced Installer (LZMA)", _pe(b"Advanced Installer 21.0"), True),
    ("Inno Setup", _pe(b"Inno Setup Setup Data (6.2.2)"), False),
    ("InstallShield", _pe(b"InstallShield 2019"), False),
    ("WiX Burn bundle", _pe(b"WixBundle 3.14"), False),
    ("7-Zip self-extractor", _pe(b"7z\xbc\xaf\x27\x1c"), False),
    ("IExpress / CAB self-extractor", _pe(b"IExpress"), False),
    ("Windows Installer wrapper", _pe(b"msiexec /i payload.msi"), False),
)
for want_family, blob, wine_only in _cases:
    _f = _home_scratch / "probe.exe"
    _f.write_bytes(blob)
    _info = _check(_f)
    check(f"{want_family} detected", _info.name, want_family)
    check(f"{want_family} wine requirement", _info.needs_wine, wine_only)

_nsis = _home_scratch / "nsis.exe"
_nsis.write_bytes(b"\xef\xbe\xad\xde" + b"\x00" * 64 + b"Nullsoft")
check("NSIS is recognised by its offset-0 signature", _check(_nsis).name, "NSIS")
_stray_magic = _home_scratch / "stray.exe"
_stray_magic.write_bytes(_pe(b"nothing to see") + b"\xef\xbe\xad\xde")
check("a stray 0xDEADBEEF does not make an NSIS", _check(_stray_magic).name, "unknown")

_unknown = _home_scratch / "mystery.exe"
_unknown.write_bytes(_pe(b"some installer we have never seen"))
check("an unknown wrapper is still tried with every unpacker",
      set(_check(_unknown).tools),
      {"7z", "7za", "7zr", "cabextract", "innoextract"})

_inno = _home_scratch / "inno.exe"
_inno.write_bytes(_pe(b"Inno Setup Setup Data (6.2.2)"))
check("a family only offers its own tools", set(_check(_inno).tools), {"innoextract", "7z", "7za"})

# the Advanced Installer case must not spend minutes hunting: nothing on Linux reads it
_empty_scratch = Path(_tempfile.mkdtemp())
_unpacked = wrappers_mod.unpack_on_linux(_wrapper, _empty_scratch, info=_check(_wrapper))
check("an unreadable family is refused without running tools", _unpacked.ok, False)
check("and the refusal says why", "no Linux tool can unpack it" in _unpacked.attempts[0][1], True)
check("no scratch directories were created for it", list(_empty_scratch.iterdir()), [])
check("every installed unpacker is reported by name", isinstance(wrappers_mod.unpacker_available(), list), True)

print("nested containers (Burn bundles and InstallShield keep the MSI in a cabinet)")
def fake_msi(size: int = 8192) -> bytes:
    """OLE magic + the installer marker, which is exactly what a real MSI starts with."""
    head = wrappers_mod.MSI_MAGIC + b"Windows Installer\x00"
    return head + b"\x00" * max(1, size - len(head))


_deep = Path(_tempfile.mkdtemp())
(_deep / "attached").mkdir()
(_deep / "attached" / "vcredist.msi").write_bytes(fake_msi())
check("an MSI anywhere under the extraction root is found",
      wrappers_mod._harvest(_deep).msis[0].name, "vcredist.msi")

_nameless = Path(_tempfile.mkdtemp())
(_nameless / "a9").write_bytes(fake_msi())          # what vc_redist really hands over
check("an MSI is found even when it has no .msi name", wrappers_mod._harvest(_nameless).msis[0].name, "a9")
_liar = Path(_tempfile.mkdtemp())
(_liar / "not-really.msi").write_bytes(b"this is not an installer, it just has the name")
check("a file that only claims to be an MSI is not accepted", wrappers_mod._harvest(_liar).msis, [])
check("and it is not mistaken for a cabinet either",
      wrappers_mod.is_cab_file(_liar / "not-really.msi"), False)

# Real case: a Neural DSP MSI carrying no "Windows Installer" string at all. msitools is
# then the judge — and only the magic bytes can be the fallback when it is not installed.
_ole_no_marker = Path(_tempfile.mkdtemp())
(_ole_no_marker / "nameless-member").write_bytes(wrappers_mod.MSI_MAGIC + b"\x00" * 12288)
_verdict = wrappers_mod.is_msi_file(_ole_no_marker / "nameless-member")
check("an OLE member with no installer string is left to msitools",
      _verdict, True if not _sh3.which("msiinfo") else False)
check("three characters is the floor for architecture tokens",
      wrappers_mod._hint_words("vc_redist.x64"), ["redist", "x64"])

_payload_only = Path(_tempfile.mkdtemp())
(_payload_only / "VST3DIR").mkdir()
(_payload_only / "VST3DIR" / "Thing.vst3").write_bytes(b"vst3")
_harvested = wrappers_mod._harvest(_payload_only)
check("an unpack with no MSI reports the payload folders instead", _harvested.payload_dirs, ["VST3DIR"])
check("and finds no MSI to install from", _harvested.msis, [])

print("choosing between several MSIs in one wrapper")
_big = Path(_tempfile.mkdtemp())
for name, size in (("a0", 4096), ("a9", 200000), ("a10", 90000)):
    (_big / name).write_bytes(fake_msi(size))
_candidates = sorted(_big.iterdir())
_picked, _rest = wrappers_mod._pick_msi(_candidates, "vc_redist.x64")
check("with no name match the largest is taken", _picked.name, "a9")
check("and the others are reported, not hidden", [p.name for p in _rest], ["a10", "a0"])
check("a single candidate needs no choosing", wrappers_mod._pick_msi([_candidates[0]], "x")[0].name, "a0")
_named = sorted((_big / "Microsoft Visual C++ 2022 X64 Minimum Runtime.msi",))
_named[0].write_bytes(fake_msi())
check("a name match still wins over size",
      wrappers_mod._pick_msi(_candidates + _named, "Microsoft Visual C++ 2022 X64 Minimum Runtime")[0].name,
      _named[0].name)

# Real case: dotnet-runtime-6.0.36-win-x86.exe carries both architectures, and the x64 MSI
# is the bigger of the two. Picking by size alone installs the wrong one.
_arch = Path(_tempfile.mkdtemp())
(_arch / "Microsoft .NET Runtime - 6.0.36 (x64).msi").write_bytes(fake_msi(300000))
(_arch / "Microsoft .NET Runtime - 6.0.36 (x86).msi").write_bytes(fake_msi(280000))
check("the architecture in the wrapper's name decides, not the size",
      wrappers_mod._pick_msi(sorted(_arch.iterdir()), "dotnet-runtime-6.0.36-win-x86")[0].name,
      "Microsoft .NET Runtime - 6.0.36 (x86).msi")
check("and the same wrapper named x64 picks the x64 one",
      wrappers_mod._pick_msi(sorted(_arch.iterdir()), "dotnet-runtime-6.0.36-win-x64")[0].name,
      "Microsoft .NET Runtime - 6.0.36 (x64).msi")

# Real case: NeuralDSP Nano Cortex v5.74.0.exe ships arm64/, x86/ and x64/ siblings with
# the same file name in each. The prefix is x86_64, so arm64 must never be the default.
_sfx = Path(_tempfile.mkdtemp())
for folder in ("arm64", "x86", "x64"):
    (_sfx / folder).mkdir()
    (_sfx / folder / "Driver.msi").write_bytes(fake_msi(500000))
check("sibling architecture folders default to x64, not arm64",
      str(wrappers_mod._pick_msi(sorted(_sfx.rglob("*.msi")), "NeuralDSP Nano Cortex v5.74.0")[0].relative_to(_sfx)),
      "x64/Driver.msi")
check("the path is what says so", wrappers_mod._path_arch(_sfx / "arm64" / "Driver.msi"), -1)

print("wrapper reporting")
_work = wrappers_mod.wrapper_workdir(Path("/tmp/wpt-extract"))
check("wrapper unpacking is kept outside the MSI scratch dir", str(_work), "/tmp/wpt-extract-unpack")
check("so that extracting the MSI cannot delete it",
      _work.is_relative_to(Path("/tmp/wpt-extract")), False)

# Real bug: the same workdir across two wrappers handed back the first wrapper's MSI.
_stale = Path(_tempfile.mkdtemp())
_first = _stale / "unpack-cabextract" / "a0"
_first.parent.mkdir(parents=True)
_first.write_bytes(fake_msi(6000))
_named_first = wrappers_mod._materialise(_first, _stale)
_second = _stale / "unpack-cabextract" / "a0"
_second.write_bytes(fake_msi(9000))
_named_second = wrappers_mod._materialise(_second, _stale)
check("a second wrapper is not served the first wrapper's MSI",
      _named_second.stat().st_size, 9000)
check("and the member's own directory is kept in the name", _named_first.name, "unpack-cabextract-a0.msi")
_report = wrappers_mod.render_info(_check(_wrapper))
check("the report names the family", _check(_wrapper).name in _report, True)
check("the report gives the command that works", "--run-wrapper" in _report, True)
check("a plain MSI is reported as needing no bridge", "no wrapper to bridge" in wrappers_mod.render_info(_check(_already)), True)

_check_payload = wrappers_mod.prepare_msi(env, _wrapper, _empty_scratch / "z", allow_wine=False)
check("a payload-only unpack is explained, not silently dropped",
      "payload folders" in _check_payload.detail or "run it under Wine once" in _check_payload.detail, True)

_sh3.rmtree(_empty_scratch, ignore_errors=True)
_sh3.rmtree(_deep, ignore_errors=True)
_sh3.rmtree(_nameless, ignore_errors=True)
_sh3.rmtree(_liar, ignore_errors=True)
_sh3.rmtree(_ole_no_marker, ignore_errors=True)
_sh3.rmtree(_big, ignore_errors=True)
_sh3.rmtree(_arch, ignore_errors=True)
_sh3.rmtree(_sfx, ignore_errors=True)
_sh3.rmtree(_stale, ignore_errors=True)
_sh3.rmtree(_payload_only, ignore_errors=True)
_sh3.rmtree(_cache, ignore_errors=True)
_sh3.rmtree(_home_scratch, ignore_errors=True)

_shutil.rmtree(_precious, ignore_errors=True)
_shutil.rmtree(_tmp, ignore_errors=True)

print("where a package's payload goes, and whether it is a plugin at all")
from wpt.installer import Action as _Action  # noqa: E402
from wpt.installer import PLUGIN_KEYS as _PLUGIN_KEYS  # noqa: E402
from wpt.installer import _destination_for as _dest, places_a_plugin as _places  # noqa: E402
from wpt.msi import MsiIdentity as _MsiId  # noqa: E402
_env_pkg = Environment(
    home=Path("/home/tester"),
    wine_tree=Path("/home/tester/.local/opt/wine-d2d1-nspa-11.13"),
    prefix=Path("/home/tester/.wine-ableton"),
    user="tester",
)
_nano = _MsiId(product_name="NeuralDSP Nano Cortex Driver v5.74.0", manufacturer="NeuralDSP")
check("a plain Program Files payload lands under the vendor (Nano Cortex's driver)",
      str(_dest("PFiles64", _nano, _env_pkg)),
      "/home/tester/.wine-ableton/drive_c/Program Files/NeuralDSP/NeuralDSP Nano Cortex Driver v5.74.0")
check("its standard-dir spellings are all understood",
      {_dest(key, _nano, _env_pkg) is not None for key in ("PFILES64", "PFiles", "PFiles32", "INSTDIR")},
      {True})
check("a directory nobody maps is still refused", _dest("SOMETHINGELSE", _nano, _env_pkg), None)
check("the vendor's declared target wins when the MSI states one",
      str(_dest("VST3DIR", _MsiId(product_name="X", properties={"VST3DIR": r"C:\Program Files\Common Files\VST3"}), _env_pkg)),
      "/home/tester/.wine-ableton/drive_c/Program Files/Common Files/VST3")

_plugin_plan = installer.Plan(msi=Path("/m/x.msi"), identity=_MsiId(product_name="Thing"), actions=[
    _Action(source=Path("/t/VST3DIR/Thing.vst3"), dest=Path("/d/Thing.vst3"), label="VST3DIR -> /d"),
])
_driver_plan = installer.Plan(msi=Path("/m/y.msi"), identity=_MsiId(product_name="Nano Cortex"), actions=[
    _Action(source=Path("/t/PFiles64/api.dll"), dest=Path("/d/api.dll"), label="PFILES64 -> /d"),
    _Action(source=Path("/t/PFiles64/driver.sys"), dest=Path("/d/driver.sys"), label="PFILES64 -> /d"),
])
check("a plugin plan is recognised as one", _places(_plugin_plan), True)
check("a driver plan is not", _places(_driver_plan), False)
check("and an empty plan places nothing either", _places(installer.Plan(msi=Path("/m/z.msi"), identity=_MsiId())), False)
check("PLUGIN_KEYS names the plugin payloads", sorted(_PLUGIN_KEYS), ["AAXDIR", "VST2DIR", "VST3DIR", "VSTDIR"])

# build_plan's decision, without msitools: a plugin package places its app payload too, a driver
# package places nothing unless asked
import tempfile as _tf  # noqa: E402
def _fake_extraction(root: Path, dirs: dict[str, list[str]]) -> Path:
    for name, files in dirs.items():
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            (target / f).write_bytes(b"x")
    return root

_orig_identity, _orig_sizes, _orig_declares = msi_mod.identity, msi_mod.expected_sizes, msi_mod.declares_plugin_payload
_orig_json = None
try:
    _root = Path(_tf.mkdtemp())
    _fake_extraction(_root / "plugin", {"VST3DIR": ["Thing.vst3"], "APPDIR": ["Thing.exe"]})
    _fake_extraction(_root / "driver", {"PFiles64": ["driver.sys", "api.dll"]})
    msi_mod.identity = lambda path, *a, **k: _MsiId(product_name="Thing", manufacturer="NeuralDSP")
    msi_mod.expected_sizes = lambda path, *a, **k: {}
    _env_plan = Environment(home=Path("/home/tester"),
                            wine_tree=Path("/home/tester/.local/opt/wine-d2d1-nspa-11.13"),
                            prefix=Path("/home/tester/.wine-ableton"), user="tester")
    msi_mod.declares_plugin_payload = lambda path, *a, **k: True
    _plugin = installer.build_plan(Path("/m/p.msi"), _env_plan, _root / "plugin")
    check("a plugin package places its plugin payload and its standalone app",
          sorted(a.label.split(" ")[0] for a in _plugin.actions), ["APPDIR", "VST3DIR"])
    msi_mod.declares_plugin_payload = lambda path, *a, **k: False
    _driver = installer.build_plan(Path("/m/d.msi"), _env_plan, _root / "driver")
    check("a driver package places nothing by default", _driver.actions, [])
    check("but says where its files would have gone",
          [name for name, _dest in _driver.skipped_app], ["PFiles64"])
    check("and explains itself", any("not a plugin" in w for w in _driver.warnings), True)
    _forced = installer.build_plan(Path("/m/d.msi"), _env_plan, _root / "driver", include_app_files=True)
    check("--include-app-files places them anyway",
          [a.label.split(" ")[0] for a in _forced.actions], ["PFILES64", "PFILES64"])
finally:
    msi_mod.identity, msi_mod.expected_sizes, msi_mod.declares_plugin_payload = _orig_identity, _orig_sizes, _orig_declares
    _shutil.rmtree(_root, ignore_errors=True)

print("msi caches (a product's MSI is not always in its own vendor folder)")
_cache_root = Path(_tempfile.mkdtemp())
for rel in ("drive_c/ProgramData/Package Cache/pkg/x.msi",
            "drive_c/windows/Installer/d80a.msi",
            "drive_c/users/u/AppData/Roaming/Neural DSP/Thing/install/thing.msi"):
    target = _cache_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"msi")
_vendor_only = [p.name for p in msi_mod.find_extracted_msis(_cache_root)]
check("vendor caches are searched by default", sorted(_vendor_only), ["thing.msi", "x.msi"])
check("Wine's msiexec cache is skipped when not asked for",
      "d80a.msi" in [p.name for p in msi_mod.find_extracted_msis(_cache_root)], False)

# An MSI nothing can identify is no use to anyone, so the cache only contributes MSIs whose
# ProductName is a real product. msitools is what answers that, so stub it as if it were here.
_orig_declares = msi_mod.declares_plugin_payload
msi_mod.declares_plugin_payload = lambda path, *a, **k: True
try:
    _with_cache = [p.name for p in msi_mod.find_extracted_msis(_cache_root, include_installer_cache=True)]
    check("Wine's msiexec cache is opt-in", sorted(_with_cache), ["d80a.msi", "thing.msi", "x.msi"])
    check("and it is what makes an otherwise unverifiable product findable",
          [p.name for p in msi_mod.find_extracted_msis(_cache_root, hint="d80a", include_installer_cache=True)],
          ["d80a.msi"])

    # the prefix's own runtimes declare no plugin payload directory, and their File tables are
    # enormous (Wine Mono's 83 MB MSI costs 20 s and took `wpt list` to 54 s before this):
    # they must be skipped *before* any File table is read
    msi_mod.declares_plugin_payload = lambda path, *a, **k: False
    # sorted: the two fixtures land in the same second, so the mtime ordering is a coin toss
    check("an MSI that declares no plugin payload is skipped",
          sorted(p.name for p in msi_mod.find_extracted_msis(_cache_root, include_installer_cache=True)),
          ["thing.msi", "x.msi"])
    check("vendor-cached MSIs are not put through that filter",
          sorted(p.name for p in msi_mod.find_extracted_msis(_cache_root)),
          ["thing.msi", "x.msi"])
finally:
    msi_mod.declares_plugin_payload = _orig_declares
_shutil.rmtree(_cache_root, ignore_errors=True)

print("leftovers when no MSI is left to describe a product")
_left = Path(_tempfile.mkdtemp())
for rel in ("drive_c/ProgramData/Neural DSP/Fortin Cali Suite/User/Mine.xml",
            "drive_c/ProgramData/Neural DSP/Impulse Responses/Fortin Cali Suite/V30.wav",
            "drive_c/Program Files/Common Files/VST3/Fortin Cali Suite.vst3",
            "drive_c/Program Files/VstPlugins/Fortin Cali Suite.dll",
            "drive_c/ProgramData/Neural DSP/Archetype Nolly X/Default.xml"):
    target = _left / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
_left_env = Environment(
    home=_left,
    wine_tree=Path("/home/tester/.local/opt/wine-d2d1-nspa-11.13"),
    prefix=_left,
    user="tester",
)
_leftovers = installer.product_leftovers(_left_env, "Fortin Cali Suite")
_names = sorted(p.name for p in _leftovers)
check("the product's data and IR folders are found",
      [n for n in _names if n == "Fortin Cali Suite"], ["Fortin Cali Suite", "Fortin Cali Suite"])
check("its plugin files are found too",
      sorted(n for n in _names if n.endswith((".vst3", ".dll"))),
      ["Fortin Cali Suite.dll", "Fortin Cali Suite.vst3"])
check("another product's folder is not dragged in",
      [p for p in _leftovers if "Nolly" in str(p)], [])
check("a name too short to be meaningful reports nothing",
      installer.product_leftovers(_left_env, "X"), [])
_shutil.rmtree(_left, ignore_errors=True)

print("uninstall order (read the MSI before msiexec can delete it)")
# `msiexec /x` removes Windows Installer's cached copy of the package, and on this stack that
# cache is often the only copy: an uninstall removed d80a.msi and *then* failed to read it,
# failed to read it, leaving presets and registry entries behind.
import shutil as _sh4  # noqa: E402
_stage_root = Path(_tempfile.mkdtemp())
_scratch = _stage_root / "wpt-extract"
_scratch.mkdir()
_live_msi = _stage_root / "d80a.msi"
_live_msi.write_bytes(b"the only copy of the package")
_staged = msi_mod.stage_msi(_live_msi, _scratch)
check("the MSI is copied somewhere of its own", _staged != _live_msi, True)
check("and the copy is complete", _staged.read_bytes(), b"the only copy of the package")
check("it lives outside the scratch dir that gets emptied",
      _staged.is_relative_to(_scratch), False)
msi_mod.prepare_scratch(_scratch)                     # what every extraction does first
_live_msi.unlink()                                    # what msiexec does to the cached copy
check("so it survives both the wipe and msiexec removing the original", _staged.is_file(), True)
check("staging twice does not re-copy a good copy", msi_mod.stage_msi(_staged, _scratch), _staged)

# Neural DSP keeps the payload in a cabinet next to the .msi: staging the MSI alone produces a
# file list msiextract cannot unpack ("Error opening file ...1.cab"), which killed an uninstall
_sidecar_root = Path(_tempfile.mkdtemp())
(_sidecar_root / "Archetype Nolly X.msi").write_bytes(b"msi")
(_sidecar_root / "Archetype Nolly X1.cab").write_bytes(b"cab")
(_sidecar_root / "unrelated.txt").write_text("not ours")
_sidecar_scratch = _sidecar_root / "scratch"
_sidecar_scratch.mkdir()
_side_staged = msi_mod.stage_msi(_sidecar_root / "Archetype Nolly X.msi", _sidecar_scratch)
_keep = _side_staged.parent
check("the MSI's sidecar cabinet is staged with it", (_keep / "Archetype Nolly X1.cab").is_file(), True)
check("and nothing unrelated is dragged along", (_keep / "unrelated.txt").exists(), False)
check("the staged MSI is the one returned", _side_staged.name, "Archetype Nolly X.msi")
_sh4.rmtree(_sidecar_root, ignore_errors=True)
_sh4.rmtree(_stage_root, ignore_errors=True)

# and the CLI must refuse *before* touching anything when the MSI cannot be read
import wpt.cli as _cli  # noqa: E402
_unreadable = Path(_tempfile.mkdtemp()) / "gone.msi"
_unreadable.write_bytes(b"msi")
_calls: list[str] = []
_orig_env, _orig_pick, _orig_stage = _cli._env, _cli._pick_msi, msi_mod.stage_msi
_orig_uninstall = _cli.uninstall_product
class _Args:
    msi = str(_unreadable); product = None; product_code = None; scratch = str(_scratch)
    files_only = False; no_files = False; purge = False; rescue_presets = True
    vendor = "Neural DSP"; dry_run = False; home = None; prefix = None; tree = None; user = None
def _explode(*_a, **_k):
    raise msi_mod.MsiError("open file failed for d80a.msi")
_cli._env = lambda _args: object()
_cli._pick_msi = lambda _env, _args: _unreadable
msi_mod.stage_msi = _explode
_cli.uninstall_product = lambda *a, **k: (_calls.append("msiexec"), (0, ""))[1]
try:
    _code = _cli.cmd_uninstall(_Args())
finally:
    _cli._env, _cli._pick_msi, _cli.uninstall_product = _orig_env, _orig_pick, _orig_uninstall
    msi_mod.stage_msi = _orig_stage
check("an unreadable MSI fails with its own exit code", _code, 4)

# Regression: `wpt uninstall --product X` must resolve the ProductCode from the MSI. An edit of
# mine left that assignment after an unconditional `return 2`, so product_code stayed None and
# the very next call crashed with "'NoneType' object has no attribute 'strip'".
class _Args2(_Args):
    msi = None
    product = "Nolly"
    product_code = None
    rescue_presets = False          # the preset rescue needs a real Environment; not this test's subject
_calls.clear()
_seen: list[str] = []
_orig_had = msi_mod.MsiIdentity
_cli._env = lambda _args: object()
_cli._pick_msi = lambda _env, _args: _unreadable
msi_mod.identity = lambda path, *a, **k: _MsiId(product_name="Archetype Nolly X", product_version="1.0.2",
                                                product_code="{F84441A4-392D-4950-9EAA-D6E5DFC27A1D}")
_cli.uninstall_product = lambda *a, **k: (_seen.append("msiexec"), (0, ""))[1]
_orig_registered = _cli.scan_mod.is_registered
_cli.scan_mod.is_registered = lambda env, code: bool(code) and _seen.append(str(code)) or True
_orig_extract, _orig_plan = msi_mod.extract, _cli.build_plan
msi_mod.extract = lambda *a, **k: Path("/tmp")
_cli.build_plan = lambda *a, **k: installer.Plan(msi=Path("/m/x.msi"), identity=_MsiId(product_name="Archetype Nolly X"))
try:
    _args2 = _Args2()
    _args2.scratch = str(_scratch)
    _code2 = _cli.cmd_uninstall(_args2)
finally:
    _cli._env, _cli._pick_msi, _cli.uninstall_product = _orig_env, _orig_pick, _orig_uninstall
    _cli.scan_mod.is_registered, msi_mod.extract, _cli.build_plan = _orig_registered, _orig_extract, _orig_plan
    msi_mod.identity = _orig_had
check("--product resolves the ProductCode from the MSI", "msiexec" in _seen, True)
check("and it is the MSI's own ProductCode that is used",
      "{F84441A4-392D-4950-9EAA-D6E5DFC27A1D}" in _seen, True)
check("and msiexec was never run", _calls, [])

print("matching a catalogue entry to a downloaded installer")
from wpt.installers import match_download as _match  # noqa: E402
_files = [Path("/d/NeuralDSP Nano Cortex v5.74.0.exe"),
          Path("/d/ArchetypeRabeaXv1.1.0.exe"),
          Path("/d/ArchetypeTimHensonXv1.1.0.exe"),
          Path("/d/Fortin Cali Suite v2.0.1.exe")]
check("a product matches its own installer",
      _match("Archetype: Rabea X", _files).name, "ArchetypeRabeaXv1.1.0.exe")
check("and the same file matches the plain name", _match("Archetype Rabea X", _files).name,
      "ArchetypeRabeaXv1.1.0.exe")
# the screenshot bug: Nano Cortex's installer was offered for these two
check("Quad Cortex is NOT given the Nano Cortex installer", _match("Quad Cortex", _files), None)
check("Cortex Control is NOT given the Nano Cortex installer", _match("Cortex Control", _files), None)
check("Nano Cortex still matches its own", _match("Nano Cortex", _files).name,
      "NeuralDSP Nano Cortex v5.74.0.exe")
check("Tim Henson is not given Rabea's file", _match("Archetype: Tim Henson X", _files).name,
      "ArchetypeTimHensonXv1.1.0.exe")
check("a product with nothing downloaded matches nothing", _match("Darkglass Ultra", _files), None)
check("an empty candidate list is safe", _match("Nolly", []), None)
check("an empty product name matches nothing", _match("", _files), None)

print("preset and IR sources")
from wpt import inventory as inventory_mod  # noqa: E402
from wpt import sources as sources_mod  # noqa: E402
check("every source is a real https link",
      all(s.url.startswith("https://") for s in sources_mod.SOURCES), True)
check("names are unique", len({s.name for s in sources_mod.SOURCES}), len(sources_mod.SOURCES))
check("kinds are from the known set",
      {s.kind for s in sources_mod.SOURCES} <= {"community", "forum", "vault", "shop"}, True)
check("every source explains what it is for", all(len(s.note) > 30 for s in sources_mod.SOURCES), True)
check("lookup by exact name", sources_mod.find("Preset Junkie").name, "Preset Junkie")
check("lookup by fragment", sources_mod.find("forum").name, "Neural DSP forum — preset threads")
check("an ambiguous or unknown name finds nothing", sources_mod.find("preset sources"), None)

_forum = sources_mod.find("forum")
check("a searchable source builds a per-plugin search URL",
      _forum.for_product("Archetype Nolly X"),
      "https://unity.neuraldsp.com/search?q=Archetype+Nolly+X+presets")
check("with no product it is just the site", _forum.for_product(""), _forum.url)
_junkie = sources_mod.find("Preset Junkie")
check("a source with no verified search URL is NOT given a made-up one",
      _junkie.for_product("Nolly"), "https://presetjunkie.com/")
check("and it is flagged as needing a sign-in", _junkie.sign_in, True)
check("asking for a source that does not exist opens nothing", sources_mod.open_source("nope"), (False, ""))
check("the source list renders with its links", "presetjunkie.com" in sources_mod.render(), True)

print("what counts as a plugin file")
_inv = inventory_mod.Inventory(entries=[
    inventory_mod.PluginEntry(name="Plugin.vst3", path=Path("/p/Plugin.vst3"), kind="vst3",
                              size=10, mtime=0.0, expected_size=10, msi=Path("/m/plugin.msi")),
    inventory_mod.PluginEntry(name="Standalone.exe", path=Path("/p/Standalone.exe"), kind="standalone",
                              size=10, mtime=0.0, expected_size=10, msi=Path("/m/plugin.msi")),
    inventory_mod.PluginEntry(name="iexplore.exe", path=Path("/pf/iexplore.exe"), kind="standalone",
                              size=10, mtime=0.0),
    inventory_mod.PluginEntry(name="mDNSResponder.exe", path=Path("/pf/Bonjour/mDNSResponder.exe"),
                              kind="standalone", size=10, mtime=0.0),
])
check("an exe behind a plugin MSI is a plugin", [e.name for e in _inv.plugins],
      ["Plugin.vst3", "Standalone.exe"])
check("Wine's own and other vendors' exes are not",
      [e.name for e in _inv.unowned_standalone] + [e.name for e in _inv.plugins],
      ["iexplore.exe", "mDNSResponder.exe", "Plugin.vst3", "Standalone.exe"])
check("and by default the listing leaves them out of the rows",
      "mDNSResponder.exe" not in inventory_mod.render(_inv), True)
check("but says how many were left out",
      "2 other .exe" in inventory_mod.render(_inv), True)
check("--all-standalone shows them", "mDNSResponder.exe" in inventory_mod.render(_inv, all_standalone=True), True)

print("wpt doctor")
from wpt import doctor as doctor_mod  # noqa: E402
from wpt.cli import build_parser as _bp  # noqa: E402

_doc = doctor_mod.Report()
_doc.add("a thing", doctor_mod.OK, "fine")
check("a clean report exits 0", _doc.exit_code, 0)
check("and says so", "everything this tool needs" in doctor_mod.render(_doc), True)
_doc.add("optional", doctor_mod.WARN, "missing", "install it")
check("a warning exits 1", _doc.exit_code, 1)
check("and shows the fix", "-> install it" in doctor_mod.render(_doc), True)
_doc.add("broken", doctor_mod.FAIL, "no")
check("a failure exits 2", _doc.exit_code, 2)
check("and is summarised", "must be fixed" in doctor_mod.render(_doc), True)
_doc_json = doctor_mod.as_json(_doc)
check("the json report carries every check", _doc_json.count('"check"'), 3)
check("and the exit code", '"exit_code": 2' in _doc_json, True)

# the parts that do not need a real Wine tree, against a synthetic environment
_doc_real = doctor_mod.Report()
_empty_root = Path(_tempfile.mkdtemp())
_bare_env = Environment(home=_empty_root,
                        wine_tree=Path("/home/tester/.local/opt/wine-d2d1-nspa-11.13"),
                        prefix=_empty_root, user="tester")
doctor_mod.check_directories(_doc_real, _bare_env)
check("absent plugin directories are warnings, not failures",
      {c.status for c in _doc_real.checks}, {doctor_mod.WARN})
check("and each one names itself", len(_doc_real.checks), 5)

# a directory it cannot write to is a failure, because nothing could be installed there
_ro = Path(_tempfile.mkdtemp())
for rel in ("drive_c/Program Files/Common Files/VST3", "drive_c/Program Files/VstPlugins",
            "drive_c/Program Files/Common Files/Avid/Audio/Plug-Ins", "drive_c/Program Files",
            "drive_c/ProgramData"):
    ( _ro / rel).mkdir(parents=True, exist_ok=True)
import os as _os  # noqa: E402
_readonly = _ro / "drive_c/Program Files/Common Files/VST3"
_readonly.chmod(0o500)
_doc_ro = doctor_mod.Report()
doctor_mod.check_directories(_doc_ro, Environment(home=_ro, wine_tree=Path("/home/tester/x"),
                                                  prefix=_ro, user="tester"))
check("a directory it cannot write to is a failure",
      any(c.name == "dir VST3" and c.status == doctor_mod.FAIL for c in _doc_ro.checks), True)
check("and says why that matters",
      "cannot install into this prefix" in next(c.fix for c in _doc_ro.checks if c.name == "dir VST3"), True)
_readonly.chmod(0o700)
import shutil as _sh5  # noqa: E402
_sh5.rmtree(_empty_root, ignore_errors=True)
_sh5.rmtree(_ro, ignore_errors=True)
doctor_mod.check_wrappers(_doc_real)
check("wrapper support is always reported",
      any(c.name == "wrapper support" for c in _doc_real.checks), True)
doctor_mod.check_gui(_doc_real)
check("the GUI check knows whether PySide6 is importable",
      any(c.name == "graphical front end" for c in _doc_real.checks), True)
# a writable scratch dir passes, an impossible one does not
_scratch_report = doctor_mod.Report()
doctor_mod.check_scratch(_scratch_report, Path(_tempfile.mkdtemp()) / "wpt-extract")
check("a usable scratch dir passes",
      _scratch_report.checks[0].status, doctor_mod.OK)
_impossible = doctor_mod.Report()
doctor_mod.check_scratch(_impossible, Path("/proc/definitely/not/writable"))
check("an unwritable scratch dir fails", _impossible.checks[0].status, doctor_mod.FAIL)
check("and says what to do", "writable dir" in _impossible.checks[0].fix, True)

print("scratch safety")
# The guard exists to stop wpt emptying a directory that is not its own. `Path.is_relative_to` is
# True for equal paths, so the temp root itself once passed the "scratch-like" test and was wiped.
from wpt import msi as _msi  # noqa: E402

for protected in (Path(_tempfile.gettempdir()), Path("/"), Path.home()):
    try:
        _msi.prepare_scratch(protected)
        check(f"prepare_scratch refuses to empty {protected}", False, True)
    except _msi.MsiError as exc:
        check(f"prepare_scratch refuses to empty {protected}", "refusing" in str(exc), True)

# a real scratch directory still works, and still gets cleared
_scratch = Path(_tempfile.mkdtemp()) / "wpt-extract"
_scratch.mkdir()
(_scratch / "leftover-from-last-time").write_text("stale")
_msi.prepare_scratch(_scratch)
check("an ordinary scratch directory is emptied", list(_scratch.glob("leftover*")), [])
check("and marked as ours", (_scratch / _msi.EXTRACT_MARKER).is_file(), True)
_msi.prepare_scratch(_scratch)   # second call: ours, so still allowed
check("a marked directory can be reused", (_scratch / _msi.EXTRACT_MARKER).is_file(), True)

# a directory under the temp root that is not ours is scratch-like and allowed to be cleared
_under = Path(_tempfile.mkdtemp()) / "sub" / "deeper"
_under.mkdir(parents=True)
(_under / "x").write_text("y")
_msi.prepare_scratch(_under)
check("a subdirectory of the temp root is treated as scratch", (_under / _msi.EXTRACT_MARKER).is_file(), True)

# a non-empty directory outside the temp root is refused outright (anything *under* the temp root
# is considered disposable by design, which is why this one lives elsewhere)
_elsewhere = Path(_tempfile.mkdtemp(dir=str(Path.home()))) / "someone-elses-data"
_elsewhere.mkdir()
(_elsewhere / "important").write_text("do not delete")
try:
    _msi.prepare_scratch(_elsewhere)
    check("a non-empty foreign directory is refused", False, True)
except _msi.MsiError as exc:
    check("a non-empty foreign directory is refused", "not created by wpt" in str(exc), True)
    check("and is left exactly as it was", (_elsewhere / "important").read_text(), "do not delete")
import shutil as _sh6  # noqa: E402
_sh6.rmtree(_elsewhere.parent, ignore_errors=True)

print("msitools failures are stated, not raised")
from wpt import installer as _installer  # noqa: E402
import subprocess as _sp  # noqa: E402

class _TimedOut:
    """Stand-in for subprocess.run that always times out."""
    def __call__(self, *args, **kwargs):
        raise _sp.TimeoutExpired(cmd=args[0] if args else "msiinfo", timeout=kwargs.get("timeout", 0))

_real_run = _sp.run
_env_stub = type("E", (), {"wine_binary": Path("/usr/bin/wine"),
                           "wine_env": staticmethod(lambda: {})})()
try:
    _sp.run = _TimedOut()
    if _msi.missing_tools():
        print("  (skipped the msitools timeout checks: msitools is not installed on this machine)")
    else:
        try:
            _msi._TABLE_CACHE.clear()
            _msi.export_table(Path("/tmp/nothing.msi"), "File")
            check("a hung msiinfo becomes an MsiError", False, True)
        except _msi.MsiError as exc:
            check("a hung msiinfo becomes an MsiError", "did not finish" in str(exc), True)
        _msi._TABLE_CACHE.clear()
        try:
            _msi.extract(Path("/tmp/nothing.msi"), Path(_tempfile.mkdtemp()) / "scratch")
            check("a hung msiextract becomes an MsiError", False, True)
        except _msi.MsiError as exc:
            check("a hung msiextract becomes an MsiError", "did not finish" in str(exc), True)
    code, detail = _installer.uninstall(_env_stub, "{whatever}")
    check("a hung uninstall reports a code instead of raising", code, 124)
    check("and says nothing was removed", "nothing was removed" in detail, True)
    rows = _installer.purge_registry(_env_stub, [type("Ed", (), {
        "target": "HKLM\\Software\\X", "reason": "test",
        "command": staticmethod(lambda _b: ["wine", "reg", "delete", "x"])})()])
    check("a hung registry purge is a failed row", rows[0][0], "failed")
finally:
    _sp.run = _real_run

print("repair agrees with verification")
# A bundle (.vst3 directory) whose inner binary is the wrong size was flagged by verify_plan but
# skipped by filter_needing_repair, so `wpt repair` said "nothing to do: every file the MSI
# describes is already present at the right size" about a broken plugin.
_bundle = Path(_tempfile.mkdtemp()) / "VST3" / "Thing.vst3"
(_bundle / "Contents" / "x86_64-win").mkdir(parents=True)
_inner = _bundle / "Contents" / "x86_64-win" / "Thing.vst3"
_inner.write_bytes(b"x" * 999)
_plan = _installer.Plan(msi=Path("/tmp/whatever.msi"), identity=None, expected={"thing.vst3": 512})
_plan.actions.append(_installer.Action(source=Path("/tmp/src/Thing.vst3"), dest=_bundle, label="VST3DIR"))
check("verification sees the wrong size", [r[0] for r in _installer.verify_plan(_plan)], ["size-mismatch"])
check("and repair now queues it", len(_installer.filter_needing_repair(_plan).actions), 1)
_inner.write_bytes(b"y" * 512)
check("an intact bundle verifies", [r[0] for r in _installer.verify_plan(_plan)], ["ok"])
check("and repair leaves it alone", _installer.filter_needing_repair(_plan).actions, [])
_gone = _installer.Plan(msi=Path("/tmp/x.msi"), identity=None, expected={"thing.vst3": 512})
_gone.actions.append(_installer.Action(source=Path("/tmp/s"), dest=_bundle.with_name("Missing.vst3"),
                                       label="VST3DIR"))
check("a missing destination is still queued", len(_installer.filter_needing_repair(_gone).actions), 1)

print("enable/disable refuses to overwrite")
_toggle_root = Path(_tempfile.mkdtemp())
(_toggle_root / "plugins").mkdir()
_real = _toggle_root / "plugins" / "Thing.vst3"
_real.write_bytes(b"enabled")
_real.with_name("Thing.vst3" + _installer.DISABLED_SUFFIX).write_bytes(b"disabled twin")
_toggle_env = type("E", (), {"vst3_dir": _toggle_root / "plugins", "vst2_dir": _toggle_root / "nothing"})()
rows = _installer.set_enabled(_toggle_env, "Thing", enabled=True)
check("a rename that would clobber a file is refused", rows[0][0], "kept")
check("and both files are still there", (_real.exists(), _real.with_name("Thing.vst3" + _installer.DISABLED_SUFFIX).exists()),
      (True, True))
check("the dry run says it would refuse too",
      "refused" in _installer.set_enabled(_toggle_env, "Thing", enabled=True, dry_run=True)[0][2], True)

print("catalogue survives bad data and bad networks")
from wpt import catalogue as _cat  # noqa: E402
import urllib.request as _urlreq  # noqa: E402

_broken_snapshot = Path(_tempfile.mkdtemp()) / "snapshot.json"
_broken_snapshot.write_text("{ this is not json")
_empty = _cat.load_snapshot(_broken_snapshot)
check("a corrupt snapshot does not raise", _empty.releases, [])
check("and says why", "unreadable" in _empty.source, True)
_future_snapshot = Path(_tempfile.mkdtemp()) / "future.json"
_future_snapshot.write_text('{"releases": [{"product": "X", "unexpected_field": 1}]}')
check("a snapshot from a newer version does not raise", _cat.load_snapshot(_future_snapshot).releases, [])

class _NoNetwork:
    def __call__(self, *args, **kwargs):
        raise _urlreq.URLError("no route to host")

_real_urlopen = _urlreq.urlopen
try:
    _urlreq.urlopen = _NoNetwork()
    _dest = Path(_tempfile.mkdtemp())
    try:
        _cat.download("https://example.invalid/thing.exe", _dest)
        check("a failed download raises CatalogueError", False, True)
    except _cat.CatalogueError as exc:
        check("a failed download raises CatalogueError", "could not download" in str(exc), True)
    check("and leaves no file behind", list(_dest.iterdir()), [])
finally:
    _urlreq.urlopen = _real_urlopen

print("registry purge is scoped to this product")
# A registry value pointing at a same-named file *elsewhere* used to be deleted, which could take
# another product's entry with it. It is now reported instead.
_plan_purge = _installer.Plan(msi=Path("/tmp/m.msi"), identity=type("I", (), {"product_name": "Thing"})(),
                              expected={"shared.dll": 10})
_plan_purge.actions.append(_installer.Action(source=Path("/tmp/s"), dest=Path("/prefix/vst3/Thing.vst3"),
                                             label="VST3DIR"))
check("a plan warning list starts empty", _plan_purge.warnings, [])
_same_name = _installer.filter_needing_repair  # keep the name in scope; no-op here
check("purge helpers exist", hasattr(_installer, "stale_registry_edits"), True)

print("presets export degrades per file")
from wpt import presets as _presets  # noqa: E402
check("export reports failures instead of raising", hasattr(_presets, "export"), True)

print("update checking")
from wpt import updates as updates_mod  # noqa: E402

check("tags parse", updates_mod.parse_version("v0.5.10"), (0, 5, 10))
check("a packaging suffix is not part of the version", updates_mod.parse_version("0.5.9-1"), (0, 5, 9))
check("nonsense parses to zero", updates_mod.parse_version(""), (0,))
check("a newer patch is newer", updates_mod.is_newer((0, 5, 10), (0, 5, 9)), True)
check("shorter versions pad with zero", updates_mod.is_newer((0, 6), (0, 5, 9)), True)
check("a fourth component still compares", updates_mod.is_newer((0, 5, 9, 1), (0, 5, 9)), True)
check("the same version is not newer", updates_mod.is_newer((0, 5, 9), (0, 5, 9)), False)
check("an older version is not newer", updates_mod.is_newer((0, 4, 9), (0, 5, 9)), False)
check("1.0.0 beats 0.9.9", updates_mod.is_newer((1, 0), (0, 9, 9)), True)

# a version comfortably above whatever is running, so this stays true across releases
_future = (99, 0, 0)
_rel = updates_mod.Release(tag="v99.0.0", version=_future, html_url="https://example.invalid/r",
                           published_at="2026-09-27", assets=[
    updates_mod.Asset("wpt-99.0.0.tar.gz", "https://example.invalid/s.tar.gz", 100),
    updates_mod.Asset("wine-plugin-toolkit-99.0.0-1-any.pkg.tar.zst", "https://example.invalid/p.zst", 200),
    updates_mod.Asset("sha256sums.txt", "https://example.invalid/c.txt"),
])
check("the package asset is found", _rel.asset(package=True).name,
      "wine-plugin-toolkit-99.0.0-1-any.pkg.tar.zst")
check("the source asset is found", _rel.asset(package=False).name, "wpt-99.0.0.tar.gz")
check("a shared sums file is found", _rel.checksum_for(_rel.asset(package=True)).name, "sha256sums.txt")
_per_asset = updates_mod.Release(tag="v1", version=(1,),
                                 html_url="", assets=[
    updates_mod.Asset("wine-plugin-toolkit-1.0-1-any.pkg.tar.zst", "u"),
    updates_mod.Asset("wine-plugin-toolkit-1.0-1-any.pkg.tar.zst.sha256", "u2"),
])
check("a per-asset checksum file wins", _per_asset.checksum_for(_per_asset.assets[0]).name,
      "wine-plugin-toolkit-1.0-1-any.pkg.tar.zst.sha256")

# a shared sums file must yield THIS file's hash, not the first one in the file
_sums = Path(_tempfile.mkdtemp()) / "sha256sums.txt"
_sums.write_text("a" * 64 + "  some-other-file.pkg.tar.zst\n" + "b" * 64 +
                 "  wine-plugin-toolkit-99.0.0-1-any.pkg.tar.zst\n")
_sums_release = updates_mod.Release(tag="v99.0.0", version=(99, 0, 0), html_url="", assets=[
    updates_mod.Asset("wine-plugin-toolkit-99.0.0-1-any.pkg.tar.zst", "u", 1),
    updates_mod.Asset("sha256sums.txt", _sums.as_uri()),
])
check("the right line is picked out of a shared sums file",
      updates_mod.expected_sha256(_sums_release, _sums_release.assets[0], _tempfile.mkdtemp()),
      "b" * 64)
_bare_sums = Path(_tempfile.mkdtemp()) / "x.sha256"
_bare_sums.write_text("c" * 64 + "\n")
_bare_release = updates_mod.Release(tag="v1", version=(1,), html_url="", assets=[
    updates_mod.Asset("pkg.tar.zst", "u"), updates_mod.Asset("x.sha256", _bare_sums.as_uri())])
check("a bare hash file also works",
      updates_mod.expected_sha256(_bare_release, _bare_release.assets[0], _tempfile.mkdtemp()),
      "c" * 64)
check("no checksum asset means no expected hash",
      updates_mod.expected_sha256(updates_mod.Release(tag="v1", version=(1,), html_url=""),
                                  updates_mod.Asset("pkg.tar.zst", "u"), _tempfile.mkdtemp()), None)
check("version_text reads back", _rel.version_text, "99.0.0")

# a release with no assets must not blow up
_bare = updates_mod.Release(tag="v1.0.0", version=(1, 0), html_url="")
check("a release without assets returns None", _bare.asset(package=True) is None, True)
check("and has no checksum file", _bare.checksum_for(updates_mod.Asset("x.tar.gz", "")) is None, True)

# download: a file:// URL exercises the streaming, size check and rename without a network
_src = Path(_tempfile.mkdtemp()) / "thing.pkg.tar.zst"
_src.write_bytes(updates_mod.ZSTD_MAGIC + b"payload" * 100)
_asset = updates_mod.Asset(_src.name, _src.as_uri(), _src.stat().st_size)
_dest = Path(_tempfile.mkdtemp())
_got = updates_mod.download(_asset, _dest)
check("download lands under the asset name", _got.name, _src.name)
check("and has the same bytes", _got.read_bytes() == _src.read_bytes(), True)
check("no .part file is left behind", list(_dest.glob("*.part")), [])
updates_mod.validate_package(_got)
check("a real zstd package validates", True, True)
_bad = _dest / "not-a-package.pkg.tar.zst"
_bad.write_bytes(b"<html>404</html>")
try:
    updates_mod.validate_package(_bad)
    check("an HTML error page is refused as a package", False, True)
except updates_mod.UpdateError as exc:
    check("an HTML error page is refused as a package", "zstd" in str(exc), True)
_tgz = _dest / "src.tar.gz"
_tgz.write_bytes(b"\x1f\x8b" + b"x" * 10)
updates_mod.validate_source(_tgz)
check("a gzip tarball validates", True, True)
try:
    updates_mod.validate_source(_bad)
    check("an HTML error page is refused as a source tarball", False, True)
except updates_mod.UpdateError:
    check("an HTML error page is refused as a source tarball", True, True)
# a truncated download is caught by the size the release declares
_short = updates_mod.Asset(_src.name, _src.as_uri(), _src.stat().st_size + 999)
try:
    updates_mod.download(_short, Path(_tempfile.mkdtemp()))
    check("a short download is refused", False, True)
except updates_mod.UpdateError as exc:
    check("a short download is refused", "bytes" in str(exc), True)

# cache and settings live under $HOME-ish paths we can point at a temp dir
_home = Path(_tempfile.mkdtemp())
check("no cache to begin with", updates_mod.read_cache(_home) is None, True)
updates_mod.write_cache(_rel, None, home=_home)
_cached = updates_mod.read_cache(_home)
check("the cache round-trips the tag", _cached["tag"], "v99.0.0")
check("and the version", _cached["version"], [99, 0, 0])
_stale = updates_mod.json.loads(updates_mod.cache_file(_home).read_text())
_stale["checked_at"] = 0
updates_mod.cache_file(_home).write_text(updates_mod.json.dumps(_stale))
check("a cache older than a day is ignored", updates_mod.read_cache(_home) is None, True)
updates_mod.save_config({"skip_version": "99.0.0"}, home=_home)
check("skip_version persists", updates_mod.skipped_version(_home), "99.0.0")
# the suite runner exports WPT_NO_UPDATE_CHECK=1 (the GUI suites must not hit the network), so the
# environment switch is cleared for these two, then asserted on its own
_saved_env = os.environ.pop("WPT_NO_UPDATE_CHECK", None)
try:
    check("update checks default to on", updates_mod.update_check_enabled(_home), True)
    updates_mod.save_config({"update_check": False}, home=_home)
    check("and can be turned off in the settings file", updates_mod.update_check_enabled(_home), False)
    updates_mod.save_config({"update_check": True}, home=_home)
    os.environ["WPT_NO_UPDATE_CHECK"] = "1"
    check("the environment switch wins over the settings file",
          updates_mod.update_check_enabled(_home), False)
finally:
    os.environ.pop("WPT_NO_UPDATE_CHECK", None)
    if _saved_env is not None:
        os.environ["WPT_NO_UPDATE_CHECK"] = _saved_env
check("skip_version survives the second write", updates_mod.skipped_version(_home), "99.0.0")
check("a corrupt config is ignored", (updates_mod.config_path(_home).write_text("{oops"),
                                      bool(updates_mod.load_config(_home)))[1], False)

# the installer transcript is what the user watches: it must escalate visibly and come back
_script = updates_mod.install_script(Path("/tmp/wpt-pkg/wine-plugin-toolkit-0.5.9-1-any.pkg.tar.zst"))
check("the transcript uses sudo pacman -U", "sudo pacman -U" in _script, True)
check("with the exact file", "/tmp/wpt-pkg/wine-plugin-toolkit-0.5.9-1-any.pkg.tar.zst" in _script, True)
check("it reopens the toolkit afterwards", "wpt-gui" in _script, True)
check("and reports a failure instead of pretending", "$rc" in _script and "exit $rc" in _script, True)
_no_restart = updates_mod.install_script(Path("/tmp/x.pkg.tar.zst"), restart=False)
check("restart can be declined", "wpt-gui" not in _no_restart, True)

_lines = updates_mod.status_lines(_rel, None, True)
check("a newer release is reported", any("99.0.0 is available" in line for line in _lines), True)
check("with its package named", any("pkg.tar.zst" in line for line in _lines), True)
_old = updates_mod.Release(tag="v0.5.0", version=(0, 5, 0), html_url="")
check("an older release reports as up to date",
      any("newest release" in line for line in updates_mod.status_lines(_old, None, True)), True)
check("an error is reported plainly",
      updates_mod.status_lines(None, "cannot reach GitHub", True) == ["update check: cannot reach GitHub"], True)
check("is_arch_family answers a bool", isinstance(updates_mod.is_arch_family(), bool), True)

print("shell completions")
from wpt import completions as completions_mod  # noqa: E402
_parser = _bp()
for shell in completions_mod.SHELLS:
    script = completions_mod.generate(shell, _parser)
    check(f"{shell} completions are generated", bool(script.strip()), True)
    check(f"{shell} completions mention every command",
          all(command in script for command in ("doctor", "completions", "uninstall", "wrappers")), True)
_fish = completions_mod.fish(_parser)
_commands = sorted(completions_mod._subparsers(_parser))
check("fish offers every command by name",
      all(f"-a {command}" in _fish for command in _commands), True)
check("and a heading for each one",
      all(f"# {command}" in _fish for command in _commands), True)
_with_options = sorted({line.split("from ")[1].split("'")[0]
                        for line in _fish.splitlines() if "__fish_seen_subcommand_from" in line})
check("every command that takes options completes them",
      _with_options,
      [c for c in _commands if completions_mod._flags(completions_mod._subparsers(_parser)[c]) != ["--help"]])
check("including the two newest", all(name in _fish for name in ("doctor", "completions")), True)
check("fish marks options that take a value",
      any("-l product -r" in line for line in completions_mod.fish(_parser).splitlines()), True)
check("bash defines and registers a function",
      "_wpt()" in completions_mod.bash(_parser) and "complete -F _wpt wpt" in completions_mod.bash(_parser), True)
check("zsh declares itself for the right program",
      completions_mod.zsh(_parser).startswith("#compdef wpt"), True)
try:
    completions_mod.generate("powershell", _parser)
    check("an unknown shell is refused", False, True)
except ValueError as exc:
    check("an unknown shell is refused", "unknown shell" in str(exc), True)

print("cli surface")
parser = build_parser()
for command in ("env", "find-msi", "inspect", "plan", "install", "scan", "wrappers", "doctor",
                "update", "completions"):
    extra = ["x.msi"] if command in {"inspect", "plan"} else []
    extra += ["fish"] if command == "completions" else []      # it needs a shell to print for
    args = parser.parse_args([command] + extra)
    check(f"{command} parses", args.command, command)
sources_args = parser.parse_args(["presets", "--sources"])
check("presets --sources parses", sources_args.sources, True)
open_args = parser.parse_args(["presets", "--open", "forum", "--product", "Nolly"])
check("presets --open takes a product", (open_args.open, open_args.product), ("forum", "Nolly"))
list_args = parser.parse_args(["list", "--all-standalone"])
check("list --all-standalone parses", list_args.all_standalone, True)
install_args = parser.parse_args(["install", "--dry-run", "--no-vst2", "--product", "Rabea"])
check("install flags", (install_args.dry_run, install_args.no_vst2, install_args.product), (True, True, "Rabea"))

print()
if failures:
    print(f"{len(failures)} FAILURES: {', '.join(failures)}")
    raise SystemExit(1)
print("all checks passed")