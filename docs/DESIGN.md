# Design and review map — `wpt`

**Scope:** the 0.6.4 implementation plus the claim-structure changes under review; this is a map
of code and outstanding questions, not a certification of every README claim. A reviewer identified
README claims that do not match code. Record any follow-up question verbatim with a code citation,
command/test result, remaining limitation and independent reviewer response before answering it.

## Data flow observed in the source

1. `wpt/cli.py` and `wpt/gui.py` are entry points. `wpt/environment.py` locates the ableton-linux tree, prefix, user and plugin directories. A supported vendor `.exe` goes through `wpt/wrappers.py` (identify, look for a cached MSI, attempt Linux unpacking, or explicitly run the wrapper under Wine); a direct MSI skips that bridge. See `wpt/wrappers.py:85–139,679–691` and the relevant CLI/GUI call sites. Declared family support does not establish real-world installer coverage.
2. `wpt/msi.py` reads the MSI tables (`payload_files` at `:155–181`, `expected_sizes` at `:191–205`). `msiextract` needs the cabinet to obtain the install payload. For a payload-less **uninstall**, `payload_directories` reads Directory/Component/File tables without the cabinet (`:247–310`).
3. `wpt/installer.py` maps extracted roots or tables-only roots through one planner (`build_plan`/`build_plan_from_tables`/`_plan_from`, `:164–307`). The tables-only plan is `destinations_only` and `apply_plan` refuses to install it (`:319–327`). `inside_prefix` resolves destinations before `apply_plan` copies them (`:52–65,328–361`). That guard does **not** cover vendor Wine side effects or the separate rescue/update/scratch paths.
4. `verify_plan` reads planned destinations after an install (`installer.py:394–419`). `filter_needing_repair` chooses actions for repair (`:430–456`). `inventory.py:107–175` inventories plugin-looking files against an index built from cached MSIs; `scan.py` diagnoses registry paths separately. Thus plan verification, inventory integrity and registry diagnosis are **three distinct verdicts**, not one interchangeable “verified” state.
5. CLI uninstall first stages/reads the MSI, then builds the plan (payload if available; tables fallback otherwise), then invokes **`msiexec /x` before preset rescue** (`cli.py:560–698`). GUI follows the same unsafe order (`gui.py:1214–1240`) with no files-only mode. That order defeats any guarantee that rescue precedes vendor-driven deletion. `remove_files` then calls `shutil.rmtree` on planned directory destinations (`installer.py:530–569`), deleting their unowned contents too. A Wine uninstall can also affect Linux paths mapped from the prefix. A safe removal needs a full side-effect boundary design, not just `inside_prefix`.

## Known claim/code mismatch and repair design (not implemented here)

The README currently asserts “every file” and “size for size”. The code cannot uphold that:

- `msi.expected_sizes` indexes by **basename**; two File-table entries named `Plugin.ico` in separate directories collapse to one expected size (`msi.py:202`).
- `verify_plan` compares an action destination's **basename** with the table keys and omits unmatched actions (`installer.py:401–419`). Directory actions (e.g. `.aaxplugin`) need checks of every intended child, not an arbitrary first recursive file with a matching name (`_measured_size`, `:377–391`).
- `filter_needing_repair` uses case-sensitive dict lookups plus `or` and treats zero as no expectation (`:440–455`). `inventory._msi_indexes` also merges different MSIs by basename using `setdefault` (`inventory.py:107–126`). Fixing only `verify_plan` would leave repair and inventory disagreeing.

**Proposed invariant for the implementation phase:** resolve each MSI File-table row to its intended destination **relative to the planned payload root** (component directory and on-disk long name), preserving an identity for two equal basenames in different folders. Verify every installed file beneath a planned directory, including bundle internals. Use the same path-indexed expectations to select repair actions and to attribute inventory size/owner to the correct MSI. Never report `ok` for a file with no unambiguous expected row; report `unverified` or an explicit ambiguity instead. Check case normalization against the target filesystem and treat zero as a valid expected size. Do not trust a merely matching basename to infer an MSI owner.

This is a design target, **not a claim that the current code does it**. If MSI metadata cannot unambiguously map an extracted entry to a File-table row, fail the verification closed and show the unresolved row rather than inventing a size.

## Acceptance evidence required before restoring the absolute claim

- A synthetic MSI/File-table fixture with two identical basenames in different directories and **different sizes**; confirm both sizes survive, the right path gets the right expectation, and a corrupt copy is rejected.
- A bundle with at least two internal files, one missing and one wrong-sized; both have distinct results, even when the top-level bundle exists. Verify and repair agree, and no-clobber preset handling remains safe.
- A zero-byte File-table row; a wrong-case name on a case-sensitive Linux filesystem; a duplicate basename across two cached MSIs. Inventory must not assign the wrong owner or return false `ok`.
- A planted user file inside a planned bundle/directory survives uninstall, or else the CLI refuses and identifies the conflict; prove preset rescue happens before **any** external `msiexec /x` action. Exercise `--files-only` separately and use a throwaway prefix. Never use a live preset directory for this test.
- Existing synthetic prefix suites and the full shipped suite runner; on the target machine, compare a representative set of MSI tables to extracted payloads and exercise install/verify/repair on copies of the prefix only. Never modify an installed release tree or a live prefix just to obtain an audit result.
- Independent adversarial read-only review against [CLAIMS.md](CLAIMS.md) and [REVIEW-BRIEF.md](REVIEW-BRIEF.md), with reviewer assertions re-run by the maintainer. Build a new tarball/package **only after** this passes, then run suites from the extracted tarball and verify the four published assets and hashes.

## Decision log

- **Evidence beats prose:** no public claim from a test that has not been run in the relevant environment, and no "verified" label from a self-report by an agent. Preserve raw command, output, exit code, fixture/version and the commit or artefact it covered.
- **No speculative generalisation:** seven Linux-route family declarations are not proof that most vendor installers work without Wine. Neural DSP Advanced Installer is the Wine-only family named in the current README.
- **Safety has multiple boundaries:** toolkit copies/removals, preset rescue, wrapper under Wine, updater and scratch cleanup must be audited separately. An external vendor installer can have effects the toolkit cannot constrain.
- **Order:** workspace -> claim structure -> tests and code fix -> independent review -> release. Until tests and the real-machine pass, do not mark the defect resolved or promise a new release.
