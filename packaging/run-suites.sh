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
# what the release checklist asks for.
set -u

TREE="${1:-$PWD}"
cd "$TREE" || { echo "no such tree: $TREE"; exit 2; }
[ -d tests ] || { echo "$TREE has no tests/ directory"; exit 2; }

export WPT_TREE="$TREE"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
missing_pyside=0
python3 -c "import PySide6" 2>/dev/null || missing_pyside=1

echo "tree: $TREE"
echo "  version: $(sed -n 's/^__version__ = "\(.*\)"/\1/p' wpt/__init__.py 2>/dev/null || echo '?')"
if [ "$missing_pyside" = 1 ]; then
    echo "  PySide6 is absent here: the GUI suites will fail rather than test anything."
    echo "  On a machine without it, run the stdlib-only suites: test_core, test_prefix_integration."
fi
echo

fails=0
ran=0
skipped=0
release_dir="${WPT_RELEASE_DIR:-$HOME/wpt-release}"
for t in tests/*.py; do
    n=$(basename "$t" .py)
    case "$n" in
        wrapper_corpus|render_tabs) continue ;;   # manual tools, not suites
    esac
    if [ "$n" = updater_e2e_check ] && [ ! -e "$release_dir/dist" ]; then
        echo "##### $n"
        echo "SKIPPED: no built release to test against (set WPT_RELEASE_DIR; looked in $release_dir)"
        echo
        skipped=$((skipped + 1))
        continue
    fi
    echo "##### $n"
    if [ "$n" = updater_e2e_check ]; then
        ( unset WPT_NO_UPDATE_CHECK
          WPT_RELEASE_DIR="$release_dir" timeout 600 python3 "$t" > "/tmp/suite-$n.log" 2>&1 )
    else
        ( export WPT_NO_UPDATE_CHECK=1
          timeout 600 python3 "$t" > "/tmp/suite-$n.log" 2>&1 )
    fi
    code=$?
    ran=$((ran + 1))
    if [ "$code" -ne 0 ]; then
        fails=$((fails + 1))
        echo "EXIT=$code  <- failed"
        tail -5 "/tmp/suite-$n.log"
    else
        echo "EXIT=0"
    fi
    echo
done

echo "suites run: $ran, skipped: $skipped, non-zero exits: $fails"
[ "$fails" -eq 0 ] || exit 1
