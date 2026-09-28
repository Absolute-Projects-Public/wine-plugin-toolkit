# Contributing to Wine Plugin Toolkit

AI assistance is disclosed in the [README](README.md). Human or AI-assisted contributions are welcome when the contributor can explain the change and provide evidence for what it claims. A model's confident answer, a passing single test, and a release checklist tick are not proof by themselves.

## A change

1. State the observed problem, environment and the exact command and output that reproduces it. If a real product is involved, remove account identifiers and machine paths from public fixtures.
2. Add a regression check that fails on the old behaviour. Implement the smallest fix in a core module where CLI and GUI can share it. Run the targeted check, then all shipped suites through `bash packaging/run-suites.sh <tree>` (the runner gives the updater suite a different environment).
3. Treat installers, Wine and deletion as external effects. Use a synthetic prefix or a copy; never treat an installed release tree as scratch, and do not run install/repair/uninstall on someone else's live prefix for review.
4. For every changed README or release-note assertion, update [the claim ledger](docs/CLAIMS.md) **before** publishing. Split an absolute into its conditions; give each a code citation and an executable proof with raw output, or qualify it as unverified/limited. Review the *whole* README for related old wording — a correction in one section can leave the same false promise elsewhere.
5. Ask an independent reviewer from another model family to use [the review brief](docs/REVIEW-BRIEF.md), starting with README-versus-code. A reviewer may supply leads, not certification: reproduce each finding and challenge claims of a clean result. Record suspected and reproduced findings separately.

## Release gate

Do not push a new public claim or publish a release while `CONTRADICTED` applies to it or its `OPEN` qualification has been silently dropped. Run the mechanical claims check once it exists, then all suites from the **extracted release tarball**, not just the working copy. Check the installer on the target machine when its environment is required. Verify the package and source tarball against their `.sha256` files and the published asset digests; download the assets back to compare them with local artefacts. Record the commands, outputs, exit codes, commit/tag and artefact hashes in the release record. Redact older release bodies without changing their listings/assets if the public prose is not meant to remain.

No version bump is needed for unpublished work. A shipped-file change after a published tag needs a new version and a re-pinned tarball hash at release time; documentation that is intentionally published only to `main` does not alter an older tag's assets. A GitHub release body is public text separate from tracked files and must be reviewed explicitly.

The acceptance test for claim C01 is in [DESIGN.md](docs/DESIGN.md); until it passes, do not say the toolkit verifies *every* placed file. The code as it stands indexes expected sizes by basename and can miss or misattribute files. If you cannot reproduce a finding in the relevant environment, say **unverified**, not “fixed.”
