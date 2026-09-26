#!/bin/bash
# Build and install-check an unreleased tree, locally.
#
# PKGBUILD builds from the GitHub tag archive (correct for everyone else, and the hash can only be
# filled in once the tag exists). For a working tree you have not tagged yet, this stages the local
# tarball where makepkg expects it and skips the checksum check.
set -eu
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import sys; sys.path.insert(0,'.'); import wpt; print(wpt.__version__)")
ROOT=$(pwd)

bash packaging/make-tarball.sh
cp "dist/wpt-$VERSION.tar.gz" .

WORK=$(mktemp -d /tmp/wpt-local-build-XXXX)
trap 'rm -rf "$WORK"' EXIT
cp PKGBUILD "$WORK/"
# makepkg looks for the source under the name PKGBUILD declares; give it the local build of it
cp "wpt-$VERSION.tar.gz" "$WORK/wine-plugin-toolkit-$VERSION.tar.gz"
cd "$WORK"
OLDPWD="$ROOT"
makepkg -f --skipchecksums "$@"

# keep every build: ~/wpt-pkg is the archive of record (0.1.0 onwards, never tidied)
ARCHIVE="${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"
mkdir -p "$ARCHIVE"
cp -v wine-plugin-toolkit-*-any.pkg.tar.zst "$ARCHIVE/"
cp -v "$OLDPWD/dist/wpt-$VERSION.tar.gz" "$ARCHIVE/" 2>/dev/null || true
ls -1 "$ARCHIVE" | tail -4
