#!/usr/bin/env python3
"""Check *mechanical* README claims; not a substitute for independent/hardware review.

Run from the repository or an extracted release tarball:
    python3 tests/readme_claims_check.py

The checks fail if current limitations disappear from the README without evidence. A checked box
is not proof that all prose is true: see
``docs/CLAIMS.md`` and the release's raw test evidence.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wpt.cli import build_parser  # noqa: E402
from wpt.completions import SHELLS, _subparsers, generate  # noqa: E402
from wpt.doctor import FAIL, WARN, Report  # noqa: E402
from wpt.wrappers import FAMILIES  # noqa: E402

README = ROOT / "README.md"
CLAIMS = ROOT / "docs" / "CLAIMS.md"
GUI = ROOT / "wpt" / "gui.py"
EXPECTED_TABS = ("Environment", "Plugins", "Download", "Pending Install", "Install MSI", "Diagnostics")
REQUIRED_LIMITS = (
    "same-size edit is not detectable",
    "A failed rescue stops the job",
    "complete backup guarantee.",
    "Wine's vendor uninstall can still remove",
    "It prunes empty parents",
)
REQUIRED_BY_SECTION = {
    "What it does": ("not a content-hash", "cached-MSI ownership scan is incomplete"),
    "Coming from Windows": ("scratch space, preset-rescue storage", "cannot prove that every user file was discovered"),
    "Presets and IRs": ("leaves files outside", "Wine's `msiexec /x` can still remove them"),
    "How it verifies": ("size is not a content hash", "every optional MSI component was installed"),
    "Scope and limitations": ("component-state-uncertain files as leftovers", "GUI has no"),
}
OLD_PROMISES = (
    r"Verifies\s+every file it placed",
    r"copies every\s+irreplaceable preset",
    r"before it removes\s+anything",
    r"--dry-run[^\n]*show every change,\s*write nothing",
    r"moves files inside your own prefix,\s*and that is all",
    r"byte-verified like everything else",
    r"never freezes mid-extract",
    r"removed every file this MSI placed",
    r"every file[^\n]*placed will be deleted",
    r"nothing outside this prefix is touched",
    r"Your own presets and settings are not touched unless",
    r"copied to ~/\.local/share/wpt/presets/ first",
    r"leave every file in place",
    r"directory is removed recursively",
)


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print("  ok", label)


def flags(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def tabs_from_ast(path: Path) -> tuple[str, ...]:
    """Parse literal addTab labels without importing PySide6 (not installed on CI/agent host)."""
    tree = ast.parse(path.read_text())
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "addTab" or len(node.args) < 2:
            continue
        label = node.args[1]
        if isinstance(label, ast.Constant) and isinstance(label.value, str):
            result.append(label.value)
    return tuple(result)


def example_lines(readme: str) -> list[str]:
    """Fenced/prompted/indented CLI lines plus inline code beginning with wpt."""
    inside = False
    found = []
    for line in readme.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside and re.match(r"^\s*(?:\$\s*)?wpt\s+", line):
            found.append(line.split(" #", 1)[0].split(" |", 1)[0].split(" >", 1)[0].strip())
        if not inside:
            found.extend(re.findall(r"`(wpt\s+[^`]+)`", line))
    return list(dict.fromkeys(found))


def visible(markdown: str) -> str:
    """HTML comments are invisible public prose, so they cannot satisfy a warning."""
    return re.sub(r"<!--.*?-->", "", markdown, flags=re.S)


def section(markdown: str, heading: str) -> str:
    start = re.search(rf"^##+ {re.escape(heading)}\s*$", markdown, flags=re.M)
    if not start:
        return ""
    level = len(re.match(r"#+", start.group()).group())
    end = re.search(rf"^#{{1,{level}}} \S", markdown[start.end():], flags=re.M)
    return markdown[start.end():start.end() + end.start()] if end else markdown[start.end():]


def missing_limits(readme: str) -> set[str]:
    public = visible(readme)
    missing = {text for text in REQUIRED_LIMITS if text not in public}
    for heading, phrases in REQUIRED_BY_SECTION.items():
        for phrase in phrases:
            if phrase not in section(public, heading):
                missing.add(f"{heading}: {phrase}")
    return missing


def old_promises(text: str) -> list[str]:
    return [rule for rule in OLD_PROMISES if re.search(rule, visible(text), re.I | re.S)]


def ledger_gaps(ledger: str) -> list[str]:
    gaps = []
    seen = set()
    for line in ledger.splitlines():
        if re.match(r"^\|{2,} C\d\d \|", line):
            gaps.append("malformed row")
            continue
        match = re.match(r"^\| (C\d\d) \|", line)
        if not match:
            continue
        claim_id = match.group(1)
        if claim_id in seen:
            gaps.append(f"duplicate {claim_id}")
        seen.add(claim_id)
        if not line.endswith("|") or line.endswith("||"):
            gaps.append("malformed row")
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        if len(cells) != 4:
            gaps.append("malformed row")
        elif "VERIFIED" in cells[3].upper() and not all(
            marker in cells[2] for marker in ("COMMAND:", "OUTPUT:")
        ):
            gaps.append(cells[0])
    return gaps


def source_strings(path: Path) -> str:
    """Only Python literals (including docstrings), not source comments about past defects."""
    return "\n".join(node.value for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str))


def validate_examples(readme: str, parser: argparse.ArgumentParser, commands: dict) -> int:
    examples = example_lines(visible(readme))
    if not examples:
        raise AssertionError("README has no CLI examples")
    for line in examples:
        try:
            args = shlex.split(line.lstrip().removeprefix("$ "))[1:]
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(args)
        except SystemExit as exc:
            # Inline `wpt enable` may name a verb without giving its required product.
            if len(args) == 1 and args[0] in commands:
                continue
            raise AssertionError(f"README syntax refused by parser: {line} (exit {exc.code})") from exc
    return len(examples)


def run() -> None:
    readme = README.read_text()
    ledger = CLAIMS.read_text()
    testing = (ROOT / "TESTING.md").read_text(encoding="utf-8")
    parser = build_parser()
    commands = _subparsers(parser)
    examples_count = validate_examples(readme, parser, commands)
    check(True, f"all {examples_count} fenced and inline CLI examples parse (syntax only)")
    check(tuple(tabs_from_ast(GUI)) == EXPECTED_TABS, "six literal GUI tabs in documented order")
    check("Six tabs" in readme, "README describes six tabs")
    contents = section(readme, "Contents")
    toc_links = set(re.findall(r"\[[^]]+\]\(#([^)]+)\)", contents))
    required_toc = {"what-it-does", "requirements", "install", "command-line", "gui",
                    "how-it-verifies", "scope-and-limitations", "development", "credits"}
    check(required_toc <= toc_links, "README contents menu links to the main sections")
    requirements = section(readme, "Requirements")
    check("shibco/ableton-linux" in requirements and "wine-d2d1-nspa-<version>" in requirements
          and "does not bundle" in requirements and "Run in Standalone" in requirements
          and "registered uninstall" in requirements and "--purge" in requirements
          and "python3 --version" in requirements,
          "README explains the required external Wine tree, Python minimum and Wine-backed operations")
    check((ROOT / "tests" / "doctor_override_check.py").is_file()
          and 'wpt --prefix "$HOME/.wine-ableton" --tree "$HOME/.local/opt/wine-d2d1-nspa-11.13" doctor' in readme
          and "Replace `11.13` with your installed tree version" in readme,
          "README doctor override example has a dedicated CLI regression test")
    check("sudo apt install python3 python3-venv msitools" in readme,
          "README Debian/Ubuntu dependency command installs Python, venv and msitools")
    check(". .venv/bin/activate" in readme,
          "README explains how to activate source-install console scripts")
    check("sudo apt install python3 python3-venv msitools" in testing
          and "python3 --version" in testing and ". .venv/bin/activate" in testing,
          "TESTING states the minimum Python version, apt dependencies and venv activation")
    pkgbuild = (ROOT / "PKGBUILD").read_text(encoding="utf-8")
    pkgver_match = re.search(r"^pkgver=(.+)$", pkgbuild, re.MULTILINE)
    pkgrel_match = re.search(r"^pkgrel=(.+)$", pkgbuild, re.MULTILINE)
    check(pkgver_match is not None and pkgrel_match is not None
          and f"VERSION={pkgver_match.group(1)}" in readme
          and f"PKGREL={pkgrel_match.group(1)}" in readme,
          "README install examples match PKGBUILD version and pkgrel")
    check('SOURCE_TARBALL="wpt-${VERSION}.tar.gz"' in readme and
          'sha256sum -c "${SOURCE_TARBALL}.sha256"' in readme and
          'SRCDEST="$HOME/Downloads" makepkg -si' in readme,
          "README builds from the exact source archive it verifies")
    check('tar xzf "$SOURCE_TARBALL"' in readme
          and 'cd "wine-plugin-toolkit-${VERSION}"' in readme
          and ".venv/bin/python -m pip install '.[gui]'" in readme,
          "README verifies, extracts and installs the cross-distro source archive")
    check("Global flags must come before the subcommand" in readme
          and "`--home` is a discovery override, not a general `HOME` override" in readme
          and "home used for Wine-tree/product discovery and the rescue-store" in readme
          and "does not change the Wine prefix" in readme
          and "or redirect the" in readme
          and "preset store used by" in readme
          and "`presets` and uninstall" in readme
          and "`doctor` may inspect a different rescue store from the one uninstall writes to" in readme
          and "`--home` is a discovery override, not a general `HOME` override" in testing
          and "standalone-log fallback" not in readme
          and "standalone-log fallback" not in testing,
          "README and TESTING distinguish global options and --home scope")
    help_text = " ".join(build_parser().format_help().split())
    check("home for Wine-tree/product discovery and doctor rescue-store check" in help_text
          and "does not change the Wine prefix" in help_text
          and "newest matching tree under --home/.local/opt" in help_text
          and "--home defaults to $HOME" in help_text,
          "CLI help explains --home and --tree scope consistently with the README")
    exit_codes_text = " ".join(readme.split())
    exit_code_claims = (
        "`1` verification, scan or system error",
        "`2` bad input",
        "`3` environment not found",
        "`4` msitools error, external-tool timeout or uninstall preflight read/safety failure",
        "`5` catalogue error",
        "`130` interrupted",
        "For uninstall, `1` includes rescue, msiexec (including timeout), post-msiexec verification, file removal or registry purge failures; unexpected system errors also return `1`",
        "Missing MSI or ProductCode input is `2`",
        "`4` covers an MSI/plan preflight read failure or safety refusal",
        "`wpt doctor` uses `0/1/2`",
    )
    check(all(claim in exit_codes_text for claim in exit_code_claims),
          "README lists all documented CLI exit codes")
    check("runner isolates `TMPDIR`, but other suites inherit `HOME`" in testing
          and "never use a live prefix" in testing,
          "TESTING warns that non-updater suites inherit the caller's environment")
    check('PACKAGE="wine-plugin-toolkit-${VERSION}-${PKGREL}-any.pkg.tar.zst"' in readme
          and 'sudo pacman -U "$PACKAGE"' in readme
          and "wine-plugin-toolkit-*.pkg.tar.zst" not in readme
          and "wpt-*.tar.gz" not in readme,
          "README install commands select one exact release asset")
    public_docs = [README, ROOT / "CHANGELOG.md", ROOT / "TESTING.md", ROOT / "CONTRIBUTING.md",
                   ROOT / "docs" / "CLAIMS.md", ROOT / "docs" / "DESIGN.md",
                   ROOT / "docs" / "REVIEW-BRIEF.md"]
    def contains_em_dash(text: str) -> bool:
        return "\u2014" in text

    check(contains_em_dash("synthetic\u2014control"),
          "em-dash detector catches a synthetic control")
    public_copy = public_docs + sorted((ROOT / "wpt").rglob("*.py"))
    check(all(not contains_em_dash(path.read_text(encoding="utf-8")) for path in public_copy),
          "shipped documentation and application source contain no em dashes")
    check("Python 3.11 or newer" in testing and "3.14 works" not in testing
          and "shibco/ableton-linux" in testing and "bin/wine" in testing
          and "python3 -m venv .venv" in testing,
          "TESTING prerequisites and install commands match README")
    check('--assets "${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"' in testing,
          "TESTING scans the actual release asset directory")
    check(bool(FAMILIES) and FAMILIES[0].name == "Advanced Installer (LZMA)" and FAMILIES[0].needs_wine,
          "Advanced Installer is first and Wine-only")
    check(all(not f.needs_wine for f in FAMILIES[1:]), "other declared wrapper families attempt Linux path")
    check("most vendor installers" not in readme, "no statistical wrapper-coverage promise")
    check("--aax" in readme and "--aax" in flags(commands["install"]), "AAX flag documented and parsed")
    for shell in SHELLS:
        text = generate(shell, parser)
        check(bool(text) and all(cmd in text for cmd in commands), f"{shell} completion contains parser commands")
    clean = Report()
    warning = Report(); warning.add("fixture", WARN)
    failure = Report(); failure.add("fixture", FAIL)
    check((clean.exit_code, warning.exit_code, failure.exit_code) == (0, 1, 2),
          "doctor clean/warn/fail exit codes 0/1/2")
    for number in range(1, 20):
        name = f"C{number:02d}"
        check(re.search(rf"^\| {name} \|", ledger, re.M) is not None, f"ledger contains {name}")
    check(not ledger_gaps(ledger), "no malformed claim rows or VERIFIED rows without command/output fields")
    c18_row = next(line for line in ledger.splitlines() if line.startswith("| C18 |"))
    mutations = (
        (ledger.replace(c18_row, "|" + c18_row), "extra leading pipe"),
        (ledger.replace(c18_row, c18_row[:-1]), "missing trailing pipe"),
        (ledger.replace(c18_row, c18_row + "|"), "doubled trailing pipe"),
        (ledger + "\n" + c18_row, "duplicate claim ID"),
        (ledger.replace(c18_row, c18_row.replace(" | ", " | unexpected | ", 1)), "extra cell"),
    )
    for mutated, description in mutations:
        check(bool(ledger_gaps(mutated)), f"ledger rejects {description}")
    check(not missing_limits(readme), "README keeps warnings in their relevant sections")
    surfaces = {"README": readme, "TESTING": (ROOT / "TESTING.md").read_text()}
    for name in ("cli", "gui", "installer"):
        surfaces[name] = source_strings(ROOT / "wpt" / f"{name}.py")
    for label, text in surfaces.items():
        check(not old_promises(text), f"{label} has no known false absolute (matched {old_promises(text)})")
    cli_uninstall = (ROOT / "wpt" / "cli.py").read_text().split("def cmd_uninstall(", 1)[1]
    gui_uninstall = (ROOT / "wpt" / "gui.py").read_text().split("def uninstall_selected(", 1)[1]
    check(cli_uninstall.index('print("warning: back up your own files first.')
          < cli_uninstall.index("code, detail = uninstall_product("),
          "CLI backup warning occurs before msiexec call")
    check(gui_uninstall.index("Back up your own data")
          < gui_uninstall.index("code, detail = uninstall_product("),
          "GUI confirmation mentions backup before msiexec call")
    # Negative controls: these changes MUST make the gate fail, without modifying disk.
    for phrase in REQUIRED_LIMITS:
        damaged = readme.replace(phrase, "[redacted]")
        check(phrase in missing_limits(damaged), f"negative control detects removal: {phrase[:40]}")
    comment_only = "<!-- " + " ".join(REQUIRED_LIMITS) + " -->"
    check(missing_limits(comment_only) != set(), "HTML comment cannot satisfy public limitations")
    check(bool(old_promises(readme + "\nVerifies every file it placed")), "old absolute detected")
    check("C01" in ledger_gaps(ledger.replace("**TARGETED TEST; OPEN real extraction and feature state.",
                                                "**VERIFIED; OPEN real extraction and feature state.")),
          "ledger status mutation is rejected")
    for bad_example in ("$ wpt repair --bogus-flag", "    wpt uninstall --nonexistent"):
        damaged = readme + "\n```bash\n" + bad_example + "\n```"
        try:
            validate_examples(damaged, parser, commands)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"false pass on fenced mutation: {bad_example}")
    try:
        validate_examples(readme + "\n`wpt list --bogus-flag`", parser, commands)
    except AssertionError:
        pass
    else:
        raise AssertionError("false pass on inline mutation")
    check(True, "prompted, indented and inline invalid examples fail")
    print("\nREADME mechanical claims: PASS (no real MSI/GUI/release behaviour certified)")


if __name__ == "__main__":
    try:
        run()
    except AssertionError as exc:
        print(f"README mechanical claims: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
