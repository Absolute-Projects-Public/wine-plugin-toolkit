# README claim ledger

This is an **audit queue, not a certificate**. The current README has not passed a complete independent line-by-line review. An entry marked `CODE` means the cited implementation supports only the stated *narrow* fact; it is not proof of real-machine behaviour. `REPRODUCED` requires a command and recorded output. `CONTRADICTED` means the public wording overclaims or is false; correct the prose or code before publishing again. `OPEN` means do not assert it as verified in an external response. Evidence belongs next to every factual submission, including an answer to a developer. Claims that span several conditions need evidence for each condition.

## How to use this record

1. Search **the entire README**, not just this ledger, for declarative promises, counts, absolutes (`every`, `never`, `only`, `nothing`, `always`), commands, safety, supported platforms and tested hardware. Add a row before adding a new promise. This initial pass groups closely related claims by section; do **not** infer that any omitted sentence is verified.
2. For each row keep the exact public wording/section, the narrow code or test evidence, a reproducible command and its real output where needed, and the status. If evidence is only a previous session's report rather than a preserved log/test, mark it `OPEN` until re-run.
3. A reviewer from a different model family reads code, README, this ledger and the actual test output; it must *challenge* the ledger. Its report is a lead until the maintainer re-runs the proof. Put the review report and the maintainer's response in the release record.
4. A release is blocked by `CONTRADICTED` rows affecting the release's public README or by unqualified `OPEN` claims. An `OPEN` claim may remain only if the README explicitly says it is **unverified/limited**, and the release notes repeat that qualification. A docs-only change on `main` is still public and follows this rule.

## Current audit (local branch; not yet published)

The anchors refer to the README in this audit branch; they are line hints, **not** evergreen citations: use the exact quotation/section when lines shift. Source lines were read in this audit; target-machine tests were not run for this pass. Evidence command output is recorded separately in the release record; `CODE` is never a claim that a command passed.

| ID | README wording/area | Evidence and boundary | Status / required next action |
|---|---|---|---|
| C01 | What it does / How it verifies: “every file” checked against MSI File table, size for size | `wpt/msi.py:191–205` builds `{f.name: f.size}`; duplicate names overwrite. `wpt/installer.py:401–419` matches only `action.dest.name`, so an unmatched placed file gets no row. Synthetic reproduction output below shows a false mismatch for a correctly sized file. | **REPRODUCED contradiction; README qualified locally.** Path-specific expectations and bundle-internal verification still need a fix/test. |
| C02 | Repair “only missing or wrong size” from cached MSI (26, 191, 395–406) | `wpt/installer.py:430–456` drops an existing destination if the basename lookup finds no size; the lookup is case-sensitive and uses `or`, so zero bytes is treated as absent. It calls `_measured_size(destination, destination.name)` for directories. Actual cached-MSI lookup is a separate path not proved by this excerpt. | **CONTRADICTED/OPEN.** Reproduce wrong-case, duplicate-name, zero-byte and bundle cases; prove the cache route. Do not claim repair/verifier agreement. |
| C03 | Wrapper claim: no Wine for “most vendor installers” (30–31) | `wpt/wrappers.py:85–139` defines eight family entries, one (`Advanced Installer`) with `needs_wine=True`; `:679–691` attempts Linux unpacking first for the others. That is *family coverage*, not the real-world frequency of vendors or guaranteed unpack success. | **OPEN (wording too broad).** Say “supported families attempt Linux extraction first”; separately document the Wine-only Neural DSP path. Real corpus results must be retained for vendor-specific promises. |
| C04 | Install/repair/uninstall safety, files and presets (What it does, Coming from Windows, Scope and limitations) | `wpt/installer.py:310–361` checks `inside_prefix` before its own copy, but `remove_files` recursively deletes a *planned directory* and everything within it (`:530–569`), not only File-table entries. `wpt/cli.py:654–698` invokes `msiexec /x` **before** preset rescue. Synthetic removal below confirms an added file inside a planned directory is deleted. | **REPRODUCED unowned-file deletion; source-confirmed rescue order; README qualified locally.** Both need a safe redesign/test before categorical promises. User backup is essential. |
| C05 | Six GUI tabs and their order (372–390) | `wpt/gui.py:254–260` calls `addTab` six times in the documented order. | **CODE.** Mechanically assert names/order; rendering and usability need GUI evidence separately. |
| C06 | Window “never freezes mid-extract” (392–393) | `wpt/gui.py:198` defines a QThread worker; `_spawn` at `:511+` uses it, but “never” cannot be proved from architecture. The previous offscreen modal-failure hang required a fix (`install_failed` at `:1554+`). | **OPEN.** Replace the absolute with a bounded worker-thread statement; run slow-extract/close/failure scenarios on the PC. |
| C07 | CLI command examples, flags and exit codes (173–217) | `wpt/cli.py:992–1145` builds the parser. Literal examples must be run in a synthetic environment; the README does **not** have a trustworthy “N commands” number. | **OPEN.** Derive subcommands and flags from the live parser; execute each documented syntax. |
| C08 | Shell completions follow the parser (355–364) | `wpt/completions.py:19–33,50–74,77–112` reads `_SubParsersAction` and option strings; `wpt/cli.py:1012–1013` exposes the command. | **CODE** for generation; actual shell installation and future drift require a check. |
| C09 | `doctor` exits 0/1/2 by check severity (333–344) | `wpt/doctor.py:59–71`: failures→2, warnings→1, else→0. | **CODE.** Test all three outcomes independently of the host's current health. |
| C10 | AAX is off by default; flag enables it (204–214) | `wpt/installer.py:169–174,258–260` defaults `include_aax=False`. CLI and GUI wiring are not proved by this function alone. | **OPEN.** Inspect/run both front ends before saying the flag works end-to-end. |
| C11 | Download, signature checks, and visible `sudo` terminal (318–331) | Requires `wpt/updates.py`, CLI/GUI dispatch, real asset checksums and a user-controlled package install. No fresh execution in this audit. | **OPEN.** Keep code-level and published-release evidence distinct. Never imply `sudo` was exercised by the agent. |
| C12 | Wrapper family table and real-corpus claims (219–282, 408–420) | Family declarations: `wpt/wrappers.py:85–139`. The README says real 7-Zip and Burn samples worked and newer Inno is not supported, but this audit did not run those corpus samples. | **OPEN runtime evidence.** Preserve sample identity, command, exact extraction result/exit and environment before claiming a family works. |
| C13 | Inventory states and registry scan (199–202, 375–390, 395–406) | `wpt/inventory.py` and `wpt/scan.py` are independent code paths; the verifier defect in C01 means “ok means every MSI-described file agrees” is not yet justified. | **OPEN/CONTRADICTED where C01 applies.** Synthetic and real prefix fixtures must pin the verdict semantics. |
| C14 | Uninstall reads MSI before `msiexec`, tables-only fallback, rescue (Scope and limitations) | `wpt/cli.py:560–639` stages and builds the plan before `msiexec`; `installer.py:190–225,319–327` keeps a tables-only plan non-installable. **Both front ends** run `msiexec` before rescue: `cli.py:654–696`, `gui.py:1214–1240`, then `remove_files` deletes a directory recursively (`installer.py:530–569`). GUI has no files-only route. | **CODE** for read-before-msiexec and tables-only install refusal; **CONTRADICTED** for rescue-before-any-removal or exact-File-table-only deletion in both front ends. GUI confirmation and CLI strings were qualified in this local branch; the underlying order is not fixed. |
| C15 | Prefix/platform requirements and dry-run promises (Requirements, Install, Scope) | `README.md` declares Python 3.11+, Linux, ableton-linux shape and `--dry-run`. `cli.py:561–639` stages and extracts to scratch before the dry-run branch; `installer.py:547–548` prints one row per planned directory, not its contents. Packaging metadata and platform matrix still need separate checks. | **CODE contradiction to former 'write nothing / every change' wording; README/TESTING qualified locally.** Check every write path and explicitly distinguish prefix changes from scratch writes. |
| C16 | Package contents and commands (67–108, 363–364) | Must be checked in a *freshly extracted* tarball and built package; inspecting the worktree cannot prove the published asset. | **OPEN until release gate.** Verify four assets and hashes against GitHub after publishing. |
| C17 | AI disclosure and release verification process (507–521) | `packaging/run-suites.sh` and `docs/REVIEW-BRIEF.md` exist locally; historical release suites are not evidence that every future change ships with a reproduced test. | **OPEN as a process promise.** Gate each release with recorded command, exit status and independent review. |

### Reproductions on a disposable prefix (audit branch)

Run from the repo root. This uses a temporary directory and a stub File-table reader: **no real MSI,
Wine prefix or personal file is touched**. It tests the core's matching/removal behaviour, not what a
specific vendor MSI would do. Command run with exit 0:

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from wpt import msi
from wpt.installer import Action, Plan, verify_plan, remove_files
with TemporaryDirectory(prefix='wpt-audit-') as tmp:
    root=Path(tmp); drive=root/'prefix'/'drive_c'; drive.mkdir(parents=True)
    fake=root/'fixture.msi'; fake.write_bytes(b'fixture')
    original=msi.payload_files
    try:
        msi.payload_files=lambda _: [msi.PayloadFile('Plugin.ico',4096,'A'),
                                     msi.PayloadFile('Plugin.ico',999999,'B')]
        sizes=msi.expected_sizes(fake,use_cache=False)
    finally: msi.payload_files=original
    correct=drive/'A'/'Plugin.ico'; correct.parent.mkdir(); correct.write_bytes(b'x'*4096)
    plan=Plan(msi=fake,identity=SimpleNamespace(),
              actions=[Action(correct,correct,'VST3DIR')],expected=sizes)
    print('C01 expected_sizes:',sizes)
    print('C01 verify_plan on correct 4096-byte file:',
          [(status,note) for status,_path,note in verify_plan(plan)])
    bundle=drive/'VST3'/'Example.vst3'; extra=bundle/'User'/'added.xml'
    extra.parent.mkdir(parents=True); extra.write_text('personal')
    removal=Plan(msi=fake,identity=SimpleNamespace(),
                 actions=[Action(bundle,bundle,'VST3DIR')])
    rows=remove_files(removal,SimpleNamespace(drive_c=drive),dry_run=False)
    print('C04 removal statuses:',[status for status,_path,_note in rows])
    print('C04 extra file survived:',extra.exists())
PY
```

```text
C01 expected_sizes: {'Plugin.ico': 999999}
C01 verify_plan on correct 4096-byte file: [('size-mismatch', 'on disk 4096, MSI says 999999')]
C04 removal statuses: ['removed']
C04 extra file survived: False
```

### README coverage map — remaining claim-bearing sections

This map prevents an audit of the first five bullets from being mistaken for the **whole README**. The sections below have **not** been accepted as collectively true. A reviewer must break each into atomic rows as needed and cite an executable check or mark it unverified.

- Opening/status and Why this exists (lines 3–49): release, license, Python/platform badges, stack, historical error codes and origin — **OPEN** where not covered by C01/C12. Historical events need preserved logs; current code alone cannot prove them.
- Requirements/install/Windows migration (50–172): package manager commands, dependencies, Wine layout, iLok, driver refusals, backup and preset rescue — **OPEN**, C04/C15/C16 apply.
- Command line and wrapper/download flows (173–317): every example, option, family, architecture choice, sign-in behaviour, catalogue freshness, preset sites — **OPEN**, C07/C10/C12 apply.
- Updates/doctor/completions (318–365): asset, cache, terminal/privilege claims, 0/1/2 exit statuses, shell output — **OPEN** except the narrow C08/C09 code facts.
- GUI/verification (366–407): tab order, each action and state, threading, every/size/bundle and test-process promises — **OPEN** except C05; C01/C02/C06/C13 have known discrepancies.
- Limitations/safety (408–461): corpus assertions, deletion order, prefix bounds, file-only/purge, scratch, hidden/visible state, license side effects — **OPEN**, C04/C12/C14 apply.
- Development/credits/disclosure (462–521): runnable examples, module ownership, shipped tests, affiliation/license, maintainer process — **OPEN** pending code, license and release checks.

**No `CLAIMS.md` row makes its public wording true by being written.** The forthcoming mechanical check should enforce anchors and the narrow code facts; real hardware/runtime evidence remains a separate human-reviewed gate.

### Independent read-only review and response (this audit branch)

An isolated reviewer on `anthropic/claude-opus-5.5` inspected the staged docs against source. Its
report is **not** an approval: it explicitly did not inspect the full README, the target machine,
Wine, or the GUI running with PySide6. The maintainer re-read `README.md:314–319`,
`wpt/gui.py:1154–1240`, `wpt/cli.py:49–69,560–698` and `wpt/installer.py:530–569` and confirmed:

- README still contained a second, unchanged “copies every ... before anything” promise. Replaced it
  with a warning in the Presets section; CLI/GUI rescue ordering is now recorded in C14 and the design.
- The GUI confirmation and CLI/GUI output made unsupported exact-removal/outside-prefix promises.
  Their **strings**, not the behaviour, were qualified locally; both now warn before removal that
  `msiexec` precedes rescue and planned directories may contain unowned files. GUI rendering remains
  untested on this host.
- Dry-run stages/extracts into scratch and previews planned directories, not all nested files. The
  README/TESTING wording was qualified. A full side-effect sweep is still open.
- The original `readme_claims_check.py` passed despite the contradictory Presets paragraph. Its
  gate now requires caveats in specific sections, checks README/TESTING/Python literal promises,
  parses prompted/indented/inline CLI examples, and fails on three deliberately invalid examples.
  This is a **known-pattern guard**, not proof of every sentence.
- The source tarball builder previously copied directories from the working tree, including possible
  untracked files. A follow-up red test showed it built successfully with a private draft under
  `docs/` (the old `cp -r` included that directory).
  `make-tarball.sh` now refuses dirty inputs and archives only `HEAD`; a disposable git fixture
  tested clean build, untracked draft, and uncommitted edit. `release.sh` also refuses a mismatched
  pin and an existing local/remote tag (tested with isolated fixtures and a live read-only 0.6.4
  lookup). **Do not publish a rebuilt 0.6.4 asset**. The next version still needs the complete
  release gate and candidate scan; no new tarball was published by this pass.
