#!/usr/bin/env python3
"""Redact the release notes of every release older than the current one.

A release *body* is published prose that no scrub reaches: `.gitignore` filters files, not the words
typed into a release description, and they stay public for every past version. This replaces them
with a single honest line, leaving each release listed and every asset untouched.

    python3 packaging/redact-releases.py [--repo owner/name] [--keep v0.6.4] [--dry-run]

Needs `gh`, authenticated for the repository.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

REDACTED = (
    "Release notes for this version have been redacted. "
    "`CHANGELOG.md` in the repository lists what each version contains."
)


def gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="Absolute-Projects-Public/wine-plugin-toolkit")
    parser.add_argument("--keep", help="tag to leave alone (default: the latest release)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    args = parser.parse_args()

    releases = json.loads(gh("api", f"repos/{args.repo}/releases?per_page=30"))
    published = [r for r in releases if not r["draft"]]
    if not published:
        print("no published releases")
        return 0
    keep = args.keep or published[0]["tag_name"]

    for rel in published:
        tag = rel["tag_name"]
        body = rel["body"] or ""
        if tag == keep:
            print(f"{tag}: kept ({len(body)} chars)")
            continue
        if body == REDACTED:
            print(f"{tag}: already redacted")
            continue
        print(f"{tag}: {len(body)} chars -> {'would redact' if args.dry_run else 'redacting'}")
        if args.dry_run:
            continue
        before = [(a["name"], a["size"], a["digest"]) for a in rel["assets"]]
        out = json.loads(gh("api", "-X", "PATCH", f"repos/{args.repo}/releases/{rel['id']}",
                            "-f", f"body={REDACTED}"))
        after = [(a["name"], a["size"], a["digest"]) for a in out["assets"]]
        print(f"    body now {len(out['body'] or '')} chars; assets unchanged: {before == after} "
              f"({len(after)} attached); still listed: {out['draft'] is False}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
