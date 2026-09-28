#!/usr/bin/env python3
"""Check *mechanical* README claims; not a substitute for independent/hardware review.

Run from the repository or an extracted release tarball:
    python3 tests/readme_claims_check.py

The checks intentionally fail if limitations discovered in 0.6.4 disappear from the README before
code and tests justify removing them. A checked box is not proof that all prose is true: see
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
    "duplicate names and bundle-internal files",
    "not yet a complete check of every placed file",
    "The CLI currently calls Wine's `msiexec /x` **before** preset rescue",
    "directory is removed **recursively**",
    "`ok` does not certify every file in a product",
)
REQUIRED_BY_SECTION = {
    "What it does": ("not yet a complete check of every placed file", "**before** preset rescue"),
    "Coming from Windows": ("scratch space, preset-rescue storage",),
    "Presets and IRs": ("Both the CLI and GUI run Wine's `msiexec /x`", "Back up your own files"),
    "How it verifies": ("not a complete per-file verification", "`ok` does not certify every file"),
    "Scope and limitations": ("directory is removed **recursively**", "GUI also runs `msiexec /x`"),
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
    for line in ledger.splitlines():
        if not re.match(r"^\| C\d\d \|", line):
            continue
        fields = line.split("|")
        if len(fields) < 5:
            gaps.append("malformed row")
        elif "VERIFIED" in fields[4].upper() and not all(x in fields[3] for x in ("COMMAND:", "OUTPUT:")):
            gaps.append(fields[1].strip())
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
    parser = build_parser()
    commands = _subparsers(parser)
    examples_count = validate_examples(readme, parser, commands)
    check(True, f"all {examples_count} fenced and inline CLI examples parse (syntax only)")
    check(tuple(tabs_from_ast(GUI)) == EXPECTED_TABS, "six literal GUI tabs in documented order")
    check("Six tabs" in readme, "README describes six tabs")
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
    for name in ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14", "C15", "C16", "C17"):
        check(re.search(rf"^\| {name} \|", ledger, re.M) is not None, f"ledger contains {name}")
    check(not ledger_gaps(ledger), "no VERIFIED ledger row without command/output fields")
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
    check("C01" in ledger_gaps(ledger.replace("**REPRODUCED contradiction", "**VERIFIED contradiction")),
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
