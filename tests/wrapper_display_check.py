"""A vendor installer must not be launched into a session where its window cannot appear.

With no display connection, a wrapper that puts up a window waits for a click that can never come:
the job looks hung for its whole timeout with nothing on screen. The toolkit now refuses up front
and says why, and this checks that refusal - and that it refuses nothing else.

    python3 tests/wrapper_display_check.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpt import wrappers as wrappers_mod  # noqa: E402
from wpt.environment import Environment  # noqa: E402
from wpt.wrappers import Family, WrapperInfo, WrapperResult  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        failures.append(label)


_SAVED = {name: os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "WPT_ALLOW_HEADLESS_WINE")}


def set_display(display=None, wayland=None, override=None) -> None:
    for name, value in (("DISPLAY", display), ("WAYLAND_DISPLAY", wayland),
                        ("WPT_ALLOW_HEADLESS_WINE", override)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


print("what counts as a display")
set_display()
check("nothing set: no display", wrappers_mod.display_available(), False)
set_display(display=":0")
check("DISPLAY alone counts", wrappers_mod.display_available(), True)
set_display(wayland="wayland-0")
check("WAYLAND_DISPLAY alone counts", wrappers_mod.display_available(), True)
set_display(override="1")
check("the override counts, for an unattended installer", wrappers_mod.display_available(), True)


# ---------------------------------------------------------------- the refusal, and what it must not stop
_FAKE_FAMILY = Family(
    name="Advanced Installer (LZMA)",
    needs_wine=True,
    note="Advanced Installer LZMA payload: no Linux tool can unpack it",
)

_work = Path(tempfile.mkdtemp(prefix="wpt-wrapper-display-"))
_wrapper = _work / "Vendor Setup.exe"
_wrapper.write_bytes(b"MZ fake wrapper")

_env = Environment(
    home=Path("/home/tester"),
    wine_tree=Path("/home/tester/.local/opt/wine-d2d1-nspa-11.13"),
    prefix=Path("/home/tester/.wine-ableton"),
    user="tester",
)

_real_identify = wrappers_mod.identify
_real_unpack = wrappers_mod.unpack_on_linux
_real_cached = wrappers_mod.cached_msis
_real_run = wrappers_mod.run_wrapper

wrappers_mod.identify = lambda path: WrapperInfo(
    path=Path(path), size=1234, is_msi=False, family=_FAKE_FAMILY
)
wrappers_mod.unpack_on_linux = lambda path, scratch, **kw: WrapperResult(
    family=_FAKE_FAMILY.name, attempts=[("7z", "handed back a blob it cannot open")]
)
wrappers_mod.cached_msis = lambda env: set()

_ran: list[Path] = []


def _fake_run(env, path, timeout=2400):
    _ran.append(Path(path))
    return 0, ""


wrappers_mod.run_wrapper = _fake_run


print()
print("with no display, a wrapper that needs one is refused, not launched")
set_display()
_ran.clear()
result = wrappers_mod.prepare_msi(_env, _wrapper, _work / "scratch", allow_wine=True)
check("the wrapper was never executed", _ran, [])
check("the result is a refusal", result.ok, False)
check("it names the missing display", "no display for its window" in result.detail, True)
check("it says what would have happened", "would appear to hang" in result.detail, True)
check("it says what to do instead", "desktop session" in result.detail, True)
check("and mentions the override", "WPT_ALLOW_HEADLESS_WINE=1" in result.detail, True)
check("the family is still reported", result.family, "Advanced Installer (LZMA)")

print()
print("with a display it goes ahead exactly as before")
set_display(display=":0")
_ran.clear()
result = wrappers_mod.prepare_msi(_env, _wrapper, _work / "scratch", allow_wine=True)
check("the wrapper ran", [p.name for p in _ran], [_wrapper.name])
check("the run happened once", len(_ran), 1)

print()
print("and the override runs it headless on purpose")
set_display(override="1")
_ran.clear()
wrappers_mod.prepare_msi(_env, _wrapper, _work / "scratch", allow_wine=True)
check("the wrapper ran anyway", len(_ran), 1)

print()
print("a wrapper that does not need Wine is unaffected")
set_display()
_msi_result = wrappers_mod.prepare_msi(
    _env, _wrapper, _work / "scratch", allow_wine=True, product_hint="Vendor Setup"
)
check("still refused here, because this fake needs Wine", _msi_result.ok, False)

wrappers_mod.identify = _real_identify
wrappers_mod.unpack_on_linux = _real_unpack
wrappers_mod.cached_msis = _real_cached
wrappers_mod.run_wrapper = _real_run
shutil.rmtree(_work, ignore_errors=True)
for name, value in _SAVED.items():
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value

print()
if failures:
    print(f"wrapper display checks: {len(failures)} FAILED")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("wrapper display checks: all passed")
