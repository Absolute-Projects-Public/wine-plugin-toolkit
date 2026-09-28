#!/usr/bin/env python3
"""Pre-publish scan: does anything identifying a person or a machine reach the public surface?

Three places matter, and a `.gitignore` only covers one of them:

  * the tracked files (grep the repository);
  * the published tarball assets (extract and grep - the shipped copy can lag the tree);
  * the GitHub release *bodies*, which are published prose that no scrub reaches and which stay
    public for every past version.

    python3 packaging/scan-identifiers.py [repo] [--assets DIR]

Findings are printed as *patterns* by name, not as surrounding text, so a report can say what was
exposed without repeating it.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PATTERNS = {
    "a home path": r"/home/[A-Za-z0-9_.-]+",
    "a Windows user path": r"[Cc]:\\users\\[A-Za-z0-9_.-]+",
    "a bare .wine prefix": r"\.wine[-_][A-Za-z0-9_.-]+",
    "an email address": r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    "a phone number": r"\b0[45]\d{1,2}\s?\d{3}\s?\d{3}\b",
}

# Prose surfaces only: source code legitimately contains its own error strings, so looking for
# "an installer error" there finds every message the program can print. This is about prose that
# quotes a real failure from a real machine.
PROSE_PATTERNS = {
    "a quoted installer failure": r"(failed \(\d|Error opening file|No such file or directory|cannot read)",
}

# Legitimate, and listing them here is the point: a scan that cries wolf on these gets ignored.
ALLOWED = (
    r"/home/tester\b",                      # the synthetic user every test fixture uses
    r"/home/x\b",                           # a dummy path in a GUI test
    r"\.wine-ableton\b",                    # the prefix name ableton-linux creates - a convention, not a machine
    r"\.wine_(binary|tree|env|prefix|user|version)[\w.]*",   # code attributes, e.g. env.wine_tree.name
    r"[\w.+-]*@users\.noreply\.github\.com",             # the publishing identity, deliberately
)


def hits(text: str, prose: bool = False) -> list[str]:
    found = []
    patterns = dict(PATTERNS)
    if prose:
        patterns.update(PROSE_PATTERNS)
    for name, pattern in patterns.items():
        for match in re.finditer(pattern, text, re.I):
            snippet = match.group(0)
            if any(re.fullmatch(allow, snippet, re.I) for allow in ALLOWED):
                continue
            found.append(name)
            break
    return found


def tracked_files(repo: Path) -> list[Path]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files"],
                         capture_output=True, text=True).stdout.split()
    return [repo / name for name in out]


def scan_files(repo: Path) -> list[tuple[str, list[str]]]:
    report = []
    for path in tracked_files(repo):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        found = hits(text, prose=path.suffix.lower() in {".md", ".txt", ".rst"})
        if found:
            report.append((str(path.relative_to(repo)), found))
    return report


def scan_tarballs(assets: Path | None) -> list[tuple[str, list[str]]]:
    report = []
    if not assets or not assets.is_dir():
        return report
    for tarball in sorted(assets.rglob("*.tar.gz")):
        try:
            with tarfile.open(tarball) as tf:
                for member in tf.getmembers():
                    if not member.isfile() or member.size > 4_000_000:
                        continue
                    handle = tf.extractfile(member)
                    if handle is None:
                        continue
                    text = handle.read().decode("utf-8", "replace")
                    found = hits(text, prose=member.name.lower().endswith((".md", ".txt", ".rst")))
                    if found:
                        report.append((f"{tarball.name}:{member.name}", found))
        except (tarfile.TarError, OSError) as exc:
            report.append((tarball.name, [f"unreadable ({exc})"]))
    return report


def scan_releases(repo_slug: str | None) -> list[tuple[str, list[str]]]:
    if not repo_slug:
        return []
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo_slug}/releases?per_page=30"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return [(repo_slug, [f"gh failed: {proc.stderr.strip()[:60]}"])]
    report = []
    for rel in json.loads(proc.stdout or "[]"):
        body = rel.get("body") or ""
        if not body:
            continue
        found = hits(body, prose=True)
        if found:
            report.append((f"release {rel['tag_name']} body ({len(body)} chars)", found))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--assets", help="directory holding release tarballs to scan too")
    parser.add_argument("--repo-slug", default="Absolute-Projects-Public/wine-plugin-toolkit",
                        help="owner/name, for scanning release bodies (needs gh)")
    parser.add_argument("--no-releases", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    assets = Path(args.assets).expanduser() if args.assets else None
    failures = 0

    for label, report in (
        ("tracked files", scan_files(repo)),
        ("tarball assets", scan_tarballs(assets)),
        ("release bodies", [] if args.no_releases else scan_releases(args.repo_slug)),
    ):
        print(f"=== {label} ===")
        if not report:
            print("  nothing identifying found")
            continue
        failures += 1
        for where, found in report:
            print(f"  {where}: {', '.join(sorted(set(found)))}")
    print()
    print("clean" if not failures else f"{failures} surface(s) need attention")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
