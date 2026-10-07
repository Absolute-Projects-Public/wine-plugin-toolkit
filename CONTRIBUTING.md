# Contributing to Wine Plugin Toolkit

AI assistance is disclosed in the [README](README.md). Human or AI-assisted contributions are welcome when the contributor can explain the change and provide evidence for what it claims. A model's confident answer, a passing single test, and a release checklist tick are not proof by themselves.

## A change

1. State the observed problem, environment and the exact command and output that reproduces it. If a real product is involved, remove account identifiers and machine paths from public fixtures.
2. Add a regression check that fails on the old behaviour. Implement the smallest fix in a core module where CLI and GUI can share it. Run the targeted check, then all shipped suites through `bash packaging/run-suites.sh .` (the runner gives the updater suite a different environment).
3. Treat installers, Wine and deletion as external effects. Use a synthetic prefix or a copy; never treat an installed release tree as scratch, and do not run install/repair/uninstall on someone else's live prefix for review.
4. For every changed README or release-note assertion, update [the claim ledger](docs/CLAIMS.md) **before** publishing. Split an absolute into its conditions; give each a code citation and an executable proof with raw output, or qualify it as unverified/limited. Review the *whole* README for related old wording. A correction in one section can leave the same false promise elsewhere.
5. Ask an independent reviewer from another model family to use [the review brief](docs/REVIEW-BRIEF.md), starting with README-versus-code. A reviewer may supply leads, not certification: reproduce each finding and challenge claims of a clean result. Record suspected and reproduced findings separately.

## Release gate

Do not push a new public claim or publish a release while `CONTRADICTED` applies to it or its `OPEN`
qualification has been silently dropped. Run `python3 tests/readme_claims_check.py`, then all suites
from the **extracted release tarball**, not just the working copy. Check the installer on the target
machine when its environment is required.

Commit every shipped file **before** building: `make-tarball.sh` refuses dirty inputs and archives
only `HEAD`. Set a new version, build the candidate, pin its hash in PKGBUILD, commit the pin and
rebuild to prove the hash is unchanged. Build the local package through `packaging/build-local.sh`;
it checks the source pin and writes package/source checksum files into `WPT_ARCHIVE_DIR` (default
`~/wpt-pkg`). Relative `WPT_ARCHIVE_DIR` and `TMPDIR` values resolve against the directory from which
the helper is called. Use the archive directory as `WPT_RELEASE_ASSET_DIR` when running the extracted
tarball suite, set `WPT_RELEASE_DIR` to the extracted source tree, and set `WPT_REQUIRE_UPDATER_E2E=1`
so missing current-version assets fail the gate. The runner verifies both asset checksum sidecars;
invalid sidecars fail even in optional mode. Run the updater CLI step on an Arch-family host. Do not
bypass makepkg checks or use legacy `make-tarball.sh --build`. `release.sh` refuses a local/remote
existing tag or a mismatched pin. Scan tracked files, the source tarball and release notes with
`python3 packaging/scan-identifiers.py . --assets "${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"`. The scanner
does not inspect `.pkg.tar.zst` files; the package is built from the scanned, pin-verified source
tarball. A skipped/failed surface is not a clean scan. Verify package/source checksum sidecars and
published asset digests; download assets back and compare them with local artifacts. Record commands,
outputs, exit codes, commit/tag and artifact hashes in the release record.
Redact older release bodies without changing their listings/assets if the public prose is not meant
to remain.

No version bump is needed for unpublished work. A shipped-file change after a published tag needs a new version and a re-pinned tarball hash at release time; documentation that is intentionally published only to `main` does not alter an older tag's assets. A GitHub release body is public text separate from tracked files and must be reviewed explicitly.

The evidence for claim C01 is listed in [the claim ledger](docs/CLAIMS.md); automated checks include `tests/file_plan_check.py`, `tests/msi_manifest_check.py` and `tests/casefold_manifest_check.py`. [The design map](docs/DESIGN.md) records the boundaries. Do not say the toolkit verifies *every* placed file unless the exact candidate passes those checks. Plans retain full root-relative File-table paths, and synthetic tests cover duplicate basenames and bundle internals. Verification remains size-based: it does not detect same-sized edits or prove Windows Installer feature/component selection. If you cannot reproduce a finding in the relevant environment, say **unverified**, not “fixed.”
