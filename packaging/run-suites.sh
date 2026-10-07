#!/bin/bash
# Run every shipped suite, giving each one the environment it actually documents.
#
# Why this exists: `updater_e2e_check` drives the real CLI, so it must run with WPT_NO_UPDATE_CHECK
# *unset* - with it set, its four CLI checks fail with "update check disabled" for reasons that have
# nothing to do with the code. Handing every suite the same environment has produced exactly that
# false failure. Everything else wants the check off so a suite never reaches the network.
#
# Run it from the tree you want tested (the GUI suites need PySide6):
#
#     bash packaging/run-suites.sh [tree]        # default: the current directory
#
# The tree defaults to $PWD so it can be pointed at an extracted release tarball - running the
# suites from the release artefact rather than the working tree is the stronger gate, and it is
# what the release checklist asks for. WPT_RELEASE_DIR defaults to that tree; optional
# WPT_RELEASE_ASSET_DIR points to the built assets (default: WPT_RELEASE_DIR/dist). Set
# WPT_REQUIRE_UPDATER_E2E=1 to fail instead of skipping when current-version assets are missing;
# present assets with invalid checksum sidecars always fail.
set -u

tmp_base="${TMPDIR:-/tmp}"
if [[ "$tmp_base" != /* ]]; then
    tmp_base="$PWD/$tmp_base"
fi
mkdir -p "$tmp_base" || { echo "cannot create TMPDIR: $tmp_base" >&2; exit 2; }
tmp_base=$(cd "$tmp_base" && pwd -P)
log_dir=$(mktemp -d "$tmp_base/wpt-suites-XXXXXX") || { echo "cannot create suite log directory in $tmp_base" >&2; exit 2; }
scratch_dir=$(mktemp -d "$tmp_base/wpt-suites-tmp-XXXXXX") || {
    rmdir "$log_dir"
    echo "cannot create suite scratch directory in $tmp_base" >&2
    exit 2
}
trap 'rm -rf -- "$scratch_dir"' EXIT
export TMPDIR="$scratch_dir"

CALLER_PWD=$(pwd -P)
TREE="${1:-$CALLER_PWD}"
if [[ "$TREE" != /* ]]; then TREE="$CALLER_PWD/$TREE"; fi
cd "$TREE" || { echo "no such tree: $TREE"; exit 2; }
TREE=$(pwd -P)
[ -d tests ] || { echo "$TREE has no tests/ directory"; exit 2; }

export WPT_TREE="$TREE"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
missing_pyside=0
python3 -c "import PySide6" 2>/dev/null || missing_pyside=1

echo "tree: $TREE"
echo "suite logs: $log_dir"
echo "  version: $(sed -n 's/^__version__ = "\(.*\)"/\1/p' wpt/__init__.py 2>/dev/null || echo '?')"
if [ "$missing_pyside" = 1 ]; then
    echo "  PySide6 is absent here: the GUI suites will fail rather than test anything."
    echo "  On a machine without it, run the stdlib-only suites: test_core, test_prefix_integration."
fi
echo

fails=0
ran=0
skipped=0
release_dir="${WPT_RELEASE_DIR:-$TREE}"
if [[ "$release_dir" != /* ]]; then release_dir="$CALLER_PWD/$release_dir"; fi
if [[ -d "$release_dir" ]]; then release_dir=$(cd "$release_dir" && pwd -P); fi
release_assets="${WPT_RELEASE_ASSET_DIR:-$release_dir/dist}"
if [[ "$release_assets" != /* ]]; then release_assets="$CALLER_PWD/$release_assets"; fi
if [[ -d "$release_assets" ]]; then release_assets=$(cd "$release_assets" && pwd -P); fi
release_version=$(sed -n 's/^pkgver=//p' "$release_dir/PKGBUILD" 2>/dev/null || true)
release_pkgrel=$(sed -n 's/^pkgrel=//p' "$release_dir/PKGBUILD" 2>/dev/null || true)
asset_checksum_valid() {
    python3 - "$release_assets" "$1" "$2" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
asset_name, sidecar_name = sys.argv[2:4]
try:
    sidecar = (root / sidecar_name).read_text(encoding="ascii")
    if not sidecar.endswith("\n") or sidecar.count("\n") != 1:
        raise ValueError("sidecar must contain exactly one line")
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n", sidecar)
    if not match or match.group(2) != asset_name:
        raise ValueError("sidecar filename or format does not match")
    digest = hashlib.sha256()
    with (root / asset_name).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != match.group(1):
        raise ValueError("asset digest does not match its sidecar")
except (OSError, UnicodeError, ValueError) as exc:
    print(f"{sidecar_name}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

missing_assets=()
if [[ -z "$release_version" || "$release_version" == *[!0-9.]* || "$release_version" != *.*.* || ! "$release_pkgrel" =~ ^[0-9]+$ ]]; then
    missing_assets+=("candidate PKGBUILD version metadata")
else
    expected_assets=(
        "wpt-$release_version.tar.gz"
        "wpt-$release_version.tar.gz.sha256"
        "wine-plugin-toolkit-$release_version-$release_pkgrel-any.pkg.tar.zst"
        "wine-plugin-toolkit-$release_version-$release_pkgrel-any.pkg.tar.zst.sha256"
    )
    for asset in "${expected_assets[@]}"; do
        [[ -f "$release_assets/$asset" ]] || missing_assets+=("$asset")
    done
fi
if [[ ! -d "$release_assets" ]]; then
    missing_assets+=("asset directory $release_assets")
fi
invalid_assets=()
if ((${#missing_assets[@]} == 0)); then
    if ! asset_checksum_valid "${expected_assets[0]}" "${expected_assets[1]}"; then
        invalid_assets+=("${expected_assets[1]}")
    fi
    if ! asset_checksum_valid "${expected_assets[2]}" "${expected_assets[3]}"; then
        invalid_assets+=("${expected_assets[3]}")
    fi
fi
updater_assets_missing=0
((${#missing_assets[@]} == 0)) || updater_assets_missing=1
updater_assets_invalid=0
((${#invalid_assets[@]} == 0)) || updater_assets_invalid=1
for t in tests/*.py; do
    n=$(basename "$t" .py)
    case "$n" in
        wrapper_corpus|render_tabs) continue ;;   # manual tools, not suites
    esac
    if [ "$n" = updater_e2e_check ] && [ "$updater_assets_invalid" = 1 ]; then
        echo "##### $n"
        invalid_summary="${invalid_assets[*]}"
        echo "FAILED: updater E2E asset checksums failed for $release_version ($invalid_summary; looked in $release_assets)"
        fails=$((fails + 1))
        echo
        continue
    fi
    if [ "$n" = updater_e2e_check ] && [ "$updater_assets_missing" = 1 ]; then
        echo "##### $n"
        missing_summary="${missing_assets[*]}"
        if [ "${WPT_REQUIRE_UPDATER_E2E:-0}" = 1 ]; then
            echo "FAILED: required updater E2E assets are missing for ${release_version:-unknown} ($missing_summary; looked in $release_assets)"
            fails=$((fails + 1))
        else
            echo "SKIPPED: current-version updater assets are missing for ${release_version:-unknown} ($missing_summary; looked in $release_assets)"
            skipped=$((skipped + 1))
        fi
        echo
        continue
    fi
    echo "##### $n"
    if [ "$n" = updater_e2e_check ]; then
        ( unset WPT_NO_UPDATE_CHECK
          WPT_RELEASE_DIR="$release_dir" WPT_RELEASE_ASSET_DIR="$release_assets" timeout 600 python3 "$t" > "$log_dir/suite-$n.log" 2>&1 )
    else
        ( export WPT_NO_UPDATE_CHECK=1
          timeout 600 python3 "$t" > "$log_dir/suite-$n.log" 2>&1 )
    fi
    code=$?
    ran=$((ran + 1))
    if [ "$code" -ne 0 ]; then
        fails=$((fails + 1))
        echo "EXIT=$code  <- failed"
        tail -5 "$log_dir/suite-$n.log"
    else
        echo "EXIT=0"
    fi
    echo
done

echo "suites run: $ran, skipped: $skipped, non-zero exits: $fails"
[ "$fails" -eq 0 ] || exit 1
